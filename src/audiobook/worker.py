import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from . import jobs
from .config import Settings

logger = logging.getLogger(__name__)

# 这些键变了就必须重建引擎 / LLM 客户端；其余的（停顿、响度、导出格式）改了下个任务自然生效
ENGINE_KEYS = (
    "engine",
    "tts_endpoints",
    "synth_concurrency",
    "synth_concurrency_max",
    "tts_timeout_seconds",
    "tts_health_cache_seconds",
)
LLM_KEYS = (
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_temperature",
    "llm_concurrency",
    "llm_timeout_seconds",
    "llm_max_attempts",
    "llm_max_output_tokens",
)


def _key(settings, names: tuple[str, ...]) -> tuple:
    values = []
    for name in names:
        value = getattr(settings, name, None)
        values.append(tuple(value) if isinstance(value, list) else value)
    return tuple(values)


@dataclass
class WorkerContext:
    settings: Settings
    conn: object
    worker_id: str
    engine: object | None = None
    llm: object | None = None
    reload_settings: Callable[[], Settings] | None = None
    engine_factory: Callable[[Settings], object] | None = None
    llm_factory: Callable[[Settings], object] | None = None

    def progress(self, job, done: int, total: int, message: str = "") -> None:
        jobs.set_progress(self.conn, job.id, done, total, message)

    def refresh(self) -> bool:
        """每轮任务前重读设置：改并发/端点/引擎、点了一键启动 TTS，都不用重启 worker。"""
        if self.reload_settings is None:
            return False
        try:
            fresh = self.reload_settings()
        except Exception as exc:  # noqa: BLE001 - 读不到就继续用旧设置
            logger.warning("重读设置失败，继续用启动时的设置：%s", exc)
            return False
        if fresh.model_dump() == self.settings.model_dump():
            return False
        previous = self.settings
        self.settings = fresh
        if self.engine is not None and _key(previous, ENGINE_KEYS) != _key(fresh, ENGINE_KEYS):
            rebuilt = self._try_build("合成引擎", self.engine_factory)
            if rebuilt is not None:
                self._close(self.engine, "合成引擎")
                self.engine = rebuilt
        if self.llm is not None and _key(previous, LLM_KEYS) != _key(fresh, LLM_KEYS):
            rebuilt = self._try_build("LLM 客户端", self.llm_factory)
            if rebuilt is not None:
                self._close(self.llm, "LLM 客户端")
                self.llm = rebuilt
        return True

    def _try_build(self, label: str, factory):
        if factory is None:
            return None
        try:
            return factory(self.settings)
        except Exception as exc:  # noqa: BLE001 - 重建失败不该让 worker 退出
            logger.warning("重建 %s 失败，继续用旧实例：%s", label, exc)
            return None

    @staticmethod
    def _close(instance, label: str) -> None:
        close = getattr(instance, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 关旧实例失败不影响新实例
                logger.warning("关闭旧的 %s 失败", label)


class Handler(Protocol):
    def __call__(self, ctx: "WorkerContext", job) -> None: ...


HANDLERS: dict[str, Callable[[WorkerContext, object], None]] = {}


def register(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


def _heartbeat_loop(ctx: WorkerContext, job_id: int, lease_seconds: int, stop: threading.Event) -> None:
    interval = max(1.0, lease_seconds / 3.0)
    while not stop.wait(interval):
        if not jobs.heartbeat(ctx.conn, job_id, ctx.worker_id, lease_seconds):
            logger.warning("心跳失败，任务 %s 可能已被回收", job_id)
            return


def run_once(ctx: WorkerContext, lease_seconds: int | None = None) -> bool:
    ctx.refresh()
    lease = lease_seconds or ctx.settings.lease_seconds
    jobs.reap_expired(ctx.conn)
    job = jobs.claim(ctx.conn, ctx.worker_id, lease)
    if job is None:
        return False
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(ctx, job.id, lease, stop), daemon=True)
    hb.start()
    try:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            raise RuntimeError(f"未注册的任务类型: {job.kind}")
        handler(ctx, job)
        jobs.complete(ctx.conn, job.id, ctx.worker_id)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库，不能让 worker 退出
        logger.exception("任务 %s 执行失败", job.id)
        jobs.fail(ctx.conn, job.id, ctx.worker_id, f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        hb.join(timeout=1.0)
    return True


def run_forever(
    ctx: WorkerContext,
    poll_seconds: float | None = None,
    stop_event=None,
    max_jobs: int | None = None,
) -> int:
    interval = poll_seconds or ctx.settings.worker_poll_seconds
    executed = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if run_once(ctx):
            executed += 1
            if max_jobs is not None and executed >= max_jobs:
                break
            continue
        if max_jobs is not None:
            break
        time.sleep(interval)
    return executed
