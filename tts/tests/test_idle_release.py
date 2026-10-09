"""空闲归还显存：只在"模型已加载 + 没在飞请求 + 静默够久"时把缓存池的空闲块还给驱动。"""

import time

import pytest

import aiab_tts.state as state_mod
from _stub_backend import StubBackend
from aiab_tts.config import TtsSettings
from aiab_tts.state import ServiceState


def _state(tmp_path, **overrides) -> ServiceState:
    settings = TtsSettings(data_dir=tmp_path / "data", **overrides)
    return ServiceState(StubBackend(), settings)


class _Pool:
    """假的缓存池读数：empty_cache 之后 reserved 掉到 allocated（块真还给驱动了）。"""

    def __init__(self, allocated=1000.0, reserved=3000.0):
        self.allocated = allocated
        self.reserved = reserved
        self.emptied = 0

    def read(self):
        return self.allocated, self.reserved

    def empty(self):
        self.emptied += 1
        self.reserved = self.allocated


@pytest.fixture
def pool(monkeypatch):
    fake = _Pool()
    monkeypatch.setattr(state_mod, "_cuda_pool_mb", fake.read)
    monkeypatch.setattr(state_mod, "_empty_cuda_cache", fake.empty)
    return fake


# 测试里用 3600s：后台线程整场测试都不会真的归还，只有手动调用会触发
LONG_IDLE = 3600


def _idle_state(tmp_path, **overrides) -> ServiceState:
    state = _state(tmp_path, idle_release_seconds=LONG_IDLE, **overrides)
    state._last_activity = time.monotonic() - LONG_IDLE * 2
    return state


def test_reclaimer_thread_only_starts_when_enabled(tmp_path):
    assert _state(tmp_path, idle_release_seconds=0)._idle_reclaimer is None
    assert _state(tmp_path, idle_release_seconds=5)._idle_reclaimer is not None


def test_idle_release_returns_free_pool_to_driver(tmp_path, pool):
    state = _idle_state(tmp_path)
    state.warmup()

    freed = state._maybe_release_idle_memory()

    assert freed == pytest.approx(2000.0)
    assert pool.emptied == 1
    assert state.idle_released_mb == pytest.approx(2000.0)
    assert state.health()["idleReleasedMB"] == pytest.approx(2000.0)


def test_idle_release_skips_recent_activity_and_inflight(tmp_path, pool):
    state = _idle_state(tmp_path)
    state.warmup()

    # 刚有请求进来（含还在闸门外排队的）→ 不还
    state._last_activity = time.monotonic()
    assert state._maybe_release_idle_memory() == 0.0

    # 静默够久，但有请求在飞 → 不还（清池子会伤到它）
    state._last_activity = time.monotonic() - LONG_IDLE * 2
    state.inflight = 1
    try:
        assert state._maybe_release_idle_memory() == 0.0
    finally:
        state.inflight = 0
    assert pool.emptied == 0


def test_idle_release_skips_small_free_pool(tmp_path, monkeypatch):
    fake = _Pool(allocated=1000.0, reserved=1100.0)  # 空闲块只有 100MB
    monkeypatch.setattr(state_mod, "_cuda_pool_mb", fake.read)
    monkeypatch.setattr(state_mod, "_empty_cuda_cache", fake.empty)
    state = _idle_state(tmp_path, idle_release_min_free_mb=256)
    state.warmup()

    assert state._maybe_release_idle_memory() == 0.0
    assert fake.emptied == 0


def test_request_resets_idle_timer(tmp_path, pool):
    state = _idle_state(tmp_path)
    state.warmup()
    ref_id = state.add_ref(b"RIFFfake", "参考")["refId"]

    state.synthesize({"text": "第一句。", "refId": ref_id})

    # 请求边界已经归还过一次（见 test_release_after_request_returns_idle_blocks），
    # 空闲归还这里只看"计时被刷新"：刚跑完请求，不该再还第二次
    assert pool.emptied == 1
    assert state._maybe_release_idle_memory() == 0.0
    assert pool.emptied == 1


def test_release_after_request_returns_idle_blocks(tmp_path, pool):
    """长任务里请求背靠背来，"空闲 20 秒"永远等不到 —— 请求边界也要把空闲块还掉。"""
    state = _state(tmp_path, idle_release_seconds=LONG_IDLE)
    state.warmup()
    ref_id = state.add_ref(b"RIFFfake", "参考")["refId"]

    state.synthesize({"text": "第一句。", "refId": ref_id})

    assert pool.emptied == 1
    assert state.idle_released_mb == pytest.approx(2000.0)


def test_release_after_request_releases_under_load_when_pool_is_fat(tmp_path, pool):
    """并发跑着也照样归还：池子里攒着 2GB 空闲块（free=2000MB）时必须还。

    回归背景：2 路批量背靠背时"没有在飞请求"的窗口根本不存在，只在空闲时归还
    等于一次都不还 —— 池子涨到 9.5GB（物理 8.2GB，溢出到共享显存）也是这原因。
    """
    state = _state(tmp_path, idle_release_seconds=LONG_IDLE)
    state.warmup()
    state.inflight = 1
    try:
        freed = state.release_after_request()
    finally:
        state.inflight = 0
    assert freed == pytest.approx(2000.0)
    assert pool.emptied == 1


def test_release_after_request_skips_under_load_when_pool_is_tight(tmp_path, monkeypatch):
    """并发中但池子没虚胖（空闲块 500MB < 1536MB）→ 不还，别白白 churn。"""
    fake = _Pool(allocated=1000.0, reserved=1500.0)
    monkeypatch.setattr(state_mod, "_cuda_pool_mb", fake.read)
    monkeypatch.setattr(state_mod, "_empty_cuda_cache", fake.empty)
    state = _idle_state(tmp_path)
    state.warmup()
    state.inflight = 1
    try:
        assert state.release_after_request() == 0.0
    finally:
        state.inflight = 0
    assert fake.emptied == 0


def test_release_after_request_has_a_cooldown(tmp_path, pool):
    state = _idle_state(tmp_path)
    state.warmup()

    assert state.release_after_request() == pytest.approx(2000.0)
    pool.reserved = 3000.0      # 池子又涨回来了，但还在冷却窗口里 → 这一轮不还
    assert state.release_after_request() == 0.0
    assert pool.emptied == 1


def test_release_after_request_can_be_turned_off(tmp_path, pool):
    state = _state(tmp_path, idle_release_seconds=LONG_IDLE, release_after_request=False)
    state.warmup()
    assert state.release_after_request() == 0.0
    assert pool.emptied == 0


def test_release_after_request_skips_small_free_pool(tmp_path, monkeypatch):
    fake = _Pool(allocated=1000.0, reserved=1100.0)   # 空闲块只有 100MB
    monkeypatch.setattr(state_mod, "_cuda_pool_mb", fake.read)
    monkeypatch.setattr(state_mod, "_empty_cuda_cache", fake.empty)
    state = _state(tmp_path, idle_release_seconds=LONG_IDLE, after_request_release_min_free_mb=256)
    state.warmup()

    assert state.release_after_request() == 0.0
    assert fake.emptied == 0


def test_unloaded_model_never_triggers_release(tmp_path, pool):
    state = _idle_state(tmp_path)
    assert state._maybe_release_idle_memory() == 0.0
    assert pool.emptied == 0
