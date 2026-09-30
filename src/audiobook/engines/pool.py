import logging
import time
from dataclasses import dataclass, field

from .errors import TtsBadRef, TtsBadRequest, TtsBusy, TtsOom, TtsUnavailable, TtsVoiceMissing
from .http_tts import HttpTtsEngine

logger = logging.getLogger(__name__)

BUSY_COOLDOWN_SECONDS = 5.0
RESTORE_AFTER = 8


@dataclass
class EndpointState:
    engine: object
    base_url: str
    ok: bool = False
    capacity: int = 0
    limit: int = 0
    inflight: int = 0
    breaker_until: float = 0.0
    success_streak: int = 0
    last_error: str | None = None
    last_health_at: float | None = None
    served: int = 0
    extra: dict = field(default_factory=dict)


class TtsPool:
    """多端点 TTS 池：并发上限来自服务端自报，OOM/5xx 降档熔断后逐级恢复。"""

    def __init__(self, endpoints, settings, engine_factory=None, clock=time.monotonic, sleep=time.sleep):
        if not endpoints:
            raise TtsUnavailable("未配置任何 TTS 端点")
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        factory = engine_factory or (lambda url: HttpTtsEngine(url, settings))
        self.states = [EndpointState(engine=factory(url), base_url=url.rstrip("/")) for url in endpoints]

    # --- 生命周期 ---

    def close(self) -> None:
        for state in self.states:
            close = getattr(state.engine, "close", None)
            if callable(close):
                close()

    def refresh(self, force: bool = False) -> None:
        now = self._clock()
        for state in self.states:
            if (
                not force
                and state.last_health_at is not None
                and now - state.last_health_at < self.settings.tts_health_cache_seconds
            ):
                continue
            try:
                health = state.engine.health()
            except Exception as exc:  # noqa: BLE001 - 任何探测失败都算端点不可用
                state.ok = False
                state.last_error = f"{type(exc).__name__}: {exc}"
                state.last_health_at = now
                continue
            state.last_health_at = now
            # unloaded 也算可用：首次合成会触发服务端 warmup
            state.ok = str(health.get("status") or "").lower() in ("ok", "loading", "unloaded")
            previous_capacity = state.capacity
            state.capacity = int(health.get("recommendedConcurrency") or 0)
            state.extra = dict(health)
            state.last_error = None
            if state.capacity <= 0:
                state.ok = False
                state.last_error = "recommendedConcurrency=0"
            if state.limit == 0 and state.ok:
                state.limit = state.capacity
            elif state.ok and state.capacity != previous_capacity:
                # 服务端重启/改过并发配置：闸门按新容量重开（针对旧进程的降档记忆不再适用）
                state.limit = state.capacity
                state.success_streak = 0

    def capabilities(self):
        self.refresh()
        for state in self.states:
            if state.ok:
                return state.engine.capabilities()
        raise TtsUnavailable("没有可用的 TTS 端点: " + self._endpoint_summary())

    def concurrency_hint(self) -> int:
        self.refresh()
        healthy = [state for state in self.states if state.ok]
        if not healthy:
            return 0
        free = sum(max(0, state.limit - state.inflight) for state in healthy)
        return max(1, free)

    def capacity_hint(self) -> int:
        """服务端自报的总并发（不是"此刻剩余几个空位"）。

        每章任务用它在开头定工作线程数：线程数要的是稳定上限，实际放行由
        _acquire 按 limit/inflight 逐次把关。若拿"剩余空位"定线程数，
        任务开始那一刻恰好在忙就会把整章锁在低并发。
        """
        self.refresh()
        healthy = [state for state in self.states if state.ok]
        if not healthy:
            return 0
        return max(1, sum(max(1, state.capacity) for state in healthy))

    def status(self) -> dict:
        return {
            "concurrency": self.concurrency_hint(),
            "endpoints": [
                {
                    "base_url": state.base_url,
                    "ok": state.ok,
                    "capacity": state.capacity,
                    "limit": state.limit,
                    "inflight": state.inflight,
                    "served": state.served,
                    "breaker_seconds_left": max(0.0, round(state.breaker_until - self._clock(), 3)),
                    "last_error": state.last_error,
                }
                for state in self.states
            ],
        }

    # --- 投递 ---

    def synthesize(self, text, voice_id, params, out_path):
        state = self._acquire()
        state.inflight += 1
        try:
            result = state.engine.synthesize(text, voice_id, params, out_path)
        except TtsOom as exc:
            self._downgrade(state, factor=0.5, cooldown=self.settings.tts_breaker_seconds, reason=str(exc))
            raise
        except TtsBusy as exc:
            self._downgrade(state, factor=None, cooldown=BUSY_COOLDOWN_SECONDS, reason=str(exc))
            raise
        except (TtsVoiceMissing, TtsBadRef, TtsBadRequest):
            # 本地缺参考音频 / 请求本身不合法：端点没问题，不能把它拖下线
            raise
        except Exception as exc:  # 连接失败/协议错：该端点标记不可用并冷却
            state.ok = False
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.breaker_until = self._clock() + self.settings.tts_breaker_seconds
            raise
        else:
            state.served += 1
            self._restore(state)
            return result
        finally:
            state.inflight -= 1

    def synthesize_batch(self, items, voice_id, out_paths):
        """同音色多条一次解码：走和单条一样的端点选择 / 降档 / 熔断逻辑。"""
        state = self._acquire()
        state.inflight += 1
        try:
            results = state.engine.synthesize_batch(items, voice_id, out_paths)
        except TtsOom as exc:
            self._downgrade(state, factor=0.5, cooldown=self.settings.tts_breaker_seconds, reason=str(exc))
            raise
        except TtsBusy as exc:
            self._downgrade(state, factor=None, cooldown=BUSY_COOLDOWN_SECONDS, reason=str(exc))
            raise
        except (TtsVoiceMissing, TtsBadRef, TtsBadRequest):
            raise
        except Exception as exc:  # 连接失败/协议错：该端点标记不可用并冷却
            state.ok = False
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.breaker_until = self._clock() + self.settings.tts_breaker_seconds
            raise
        else:
            state.served += 1
            self._restore(state)
            return results
        finally:
            state.inflight -= 1

    def _acquire(self, timeout: float | None = None) -> EndpointState:
        self.refresh()
        deadline = self._clock() + (timeout or self.settings.tts_timeout_seconds)
        while True:
            healthy = [state for state in self.states if state.ok]
            if not healthy:
                raise TtsUnavailable("所有 TTS 端点均不可用: " + self._endpoint_summary())
            now = self._clock()
            ready = [
                state
                for state in healthy
                if state.breaker_until <= now and state.inflight < max(1, state.limit)
            ]
            if ready:
                ready.sort(key=lambda state: (state.inflight / max(1, state.limit), state.base_url))
                return ready[0]
            if self._clock() >= deadline:
                raise TtsBusy("所有 TTS 端点都在忙或处于冷却期")
            self._sleep(0.05)
            self.refresh(force=True)

    def _downgrade(self, state: EndpointState, *, factor: float | None, cooldown: float, reason: str) -> None:
        state.limit = max(1, int(state.limit * factor)) if factor else max(1, state.limit - 1)
        state.success_streak = 0
        state.breaker_until = self._clock() + cooldown
        state.last_error = reason
        logger.warning("TTS 端点 %s 降档至 %s（冷却 %.0fs）：%s", state.base_url, state.limit, cooldown, reason)

    def _restore(self, state: EndpointState) -> None:
        state.success_streak += 1
        if state.success_streak >= RESTORE_AFTER and state.limit < state.capacity:
            state.limit += 1
            state.success_streak = 0

    def _endpoint_summary(self) -> str:
        return "; ".join(f"{state.base_url}={state.last_error}" for state in self.states)
