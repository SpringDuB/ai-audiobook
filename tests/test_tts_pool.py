from pathlib import Path

import pytest

from audiobook.engines.base import AudioResult, EngineCapabilities, SynthParams
from audiobook.engines.errors import TtsBusy, TtsOom, TtsUnavailable, TtsVoiceMissing
from audiobook.engines.pool import TtsPool


def _caps() -> EngineCapabilities:
    return EngineCapabilities(
        name="fake-tts",
        version="1",
        emotions=True,
        emotion_dims=("happy",),
        rate=True,
        pronunciation=True,
        sample_rate=22050,
        max_text_chars=300,
    )


class FakeEndpoint:
    def __init__(self, capacity: int = 2, error: Exception | None = None, status: str = "ok"):
        self.capacity = capacity
        self.error = error
        self.status = status
        self.calls = 0
        self.health_calls = 0

    def health(self) -> dict:
        self.health_calls += 1
        return {"status": self.status, "recommendedConcurrency": self.capacity, "inflight": 0}

    def capabilities(self) -> EngineCapabilities:
        return _caps()

    def synthesize(self, text, voice_id, params, out_path) -> AudioResult:
        self.calls += 1
        if self.error:
            raise self.error
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")
        return AudioResult(path=Path(out_path), duration=0.1, sample_rate=22050)

    def close(self) -> None:
        pass


def _pool(settings, endpoints, clock=None) -> tuple[TtsPool, list[FakeEndpoint]]:
    queue = list(endpoints)
    pool = TtsPool(
        [f"http://e{index}.local" for index in range(len(endpoints))],
        settings,
        engine_factory=lambda url: queue.pop(0),
        clock=clock or (lambda: 0.0),
    )
    return pool, [state.engine for state in pool.states]


def _out(settings, name: str = "a.wav") -> Path:
    return Path(settings.data_dir) / name


def test_refresh_reads_capacity_and_health(settings):
    pool, endpoints = _pool(settings, [FakeEndpoint(3), FakeEndpoint(1)])
    pool.refresh()
    assert pool.concurrency_hint() == 4
    assert [endpoint.health_calls for endpoint in endpoints] == [1, 1]
    assert pool.capabilities().name == "fake-tts"


def test_oom_downgrades_limit_and_opens_breaker(settings):
    now = {"t": 0.0}
    pool, endpoints = _pool(
        settings,
        [FakeEndpoint(4, error=TtsOom("oom")), FakeEndpoint(2)],
        clock=lambda: now["t"],
    )
    pool.refresh()

    with pytest.raises(TtsOom):
        pool.synthesize("第一句。", "v", SynthParams(), _out(settings))

    state = pool.states[0]
    assert state.limit == 2
    assert state.breaker_until == pytest.approx(60.0)

    # 熔断期内第二次调用必须落到另一个端点
    now["t"] = 1.0
    pool.synthesize("第二句。", "v", SynthParams(), _out(settings, "b.wav"))
    assert endpoints[0].calls == 1 and endpoints[1].calls == 1


def test_busy_downgrades_by_one(settings):
    pool, _ = _pool(settings, [FakeEndpoint(4, error=TtsBusy("busy"))])
    pool.refresh()
    with pytest.raises(TtsBusy):
        pool.synthesize("第一句。", "v", SynthParams(), _out(settings))
    assert pool.states[0].limit == 3


def test_local_voice_missing_does_not_take_endpoint_down(settings):
    """缺参考音频是本地问题，端点必须保持健康（否则第一次失败就把服务拖下线）。"""
    pool, endpoints = _pool(settings, [FakeEndpoint(2, error=TtsVoiceMissing("缺少参考音频"))])
    pool.refresh()
    for index in range(3):
        with pytest.raises(TtsVoiceMissing):
            pool.synthesize("第一句。", "v", SynthParams(), _out(settings, f"{index}.wav"))
    assert pool.states[0].ok is True
    assert pool.states[0].limit == 2
    assert pool.concurrency_hint() == 2
    assert endpoints[0].calls == 3


def test_successes_restore_limit_gradually(settings):
    pool, _ = _pool(settings, [FakeEndpoint(4)])
    pool.refresh()
    pool.states[0].limit = 2
    for index in range(9):
        pool.synthesize(f"第{index}句。", "v", SynthParams(), _out(settings, f"{index}.wav"))
    assert pool.states[0].limit == 3


def test_capacity_change_reopens_the_gate(settings):
    """服务端重启并把并发从 1 调到 3：闸门要跟着开，不能一直卡在 1。"""
    endpoint = FakeEndpoint(1)
    pool, _ = _pool(settings, [endpoint])
    pool.refresh()
    assert pool.states[0].limit == 1

    endpoint.capacity = 3
    pool.refresh(force=True)
    assert pool.states[0].limit == 3
    assert pool.capacity_hint() == 3


def test_capacity_hint_ignores_current_inflight(settings):
    """定工作线程数要用总容量：此刻只剩 1 个空位不代表整章只能跑 1 路。"""
    pool, _ = _pool(settings, [FakeEndpoint(3)])
    pool.refresh()
    pool.states[0].inflight = 2
    assert pool.concurrency_hint() == 1     # 当前只剩 1 个空位
    assert pool.capacity_hint() == 3        # 总容量仍是 3


def test_all_endpoints_down_raises_unavailable(settings):
    pool, _ = _pool(settings, [FakeEndpoint(0, status="error")])
    pool.refresh()
    with pytest.raises(TtsUnavailable):
        pool.synthesize("第一句。", "v", SynthParams(), _out(settings))


def test_unloaded_endpoint_is_still_usable(settings):
    """冷启动的服务必须算可用，否则永远触发不了首次 warmup。"""
    pool, endpoints = _pool(settings, [FakeEndpoint(2, status="unloaded")])
    pool.refresh()
    assert pool.status()["endpoints"][0]["ok"] is True
    pool.synthesize("第一句。", "v", SynthParams(), _out(settings))
    assert endpoints[0].calls == 1


def test_full_endpoint_is_skipped(settings):
    pool, endpoints = _pool(settings, [FakeEndpoint(1), FakeEndpoint(1)])
    pool.refresh()
    pool.states[0].inflight = 1  # 端点 0 已满
    assert pool.concurrency_hint() == 1
    pool.synthesize("第一句。", "v", SynthParams(), _out(settings))
    assert endpoints[0].calls == 0 and endpoints[1].calls == 1


def test_status_exposes_endpoint_details(settings):
    pool, _ = _pool(settings, [FakeEndpoint(2)])
    pool.refresh()
    payload = pool.status()
    assert payload["endpoints"][0]["capacity"] == 2
    assert payload["concurrency"] == 2
    assert payload["endpoints"][0]["ok"] is True
