import pytest

from audiobook.llm.limiter import AdaptiveLimiter


def test_rate_limit_halves_limit_and_floors_at_min():
    limiter = AdaptiveLimiter(max_concurrency=8, min_concurrency=1)
    assert limiter.limit == 8
    assert limiter.record_rate_limit() == 4
    assert limiter.record_rate_limit() == 2
    assert limiter.record_rate_limit() == 1
    assert limiter.record_rate_limit() == 1


def test_success_streak_restores_limit_one_step_at_a_time():
    limiter = AdaptiveLimiter(max_concurrency=8, restore_after=3)
    limiter.record_rate_limit()
    assert limiter.limit == 4
    for _ in range(3):
        limiter.record_success()
    assert limiter.limit == 5
    for _ in range(9):
        limiter.record_success()
    assert limiter.limit == 8
    for _ in range(9):
        limiter.record_success()
    assert limiter.limit == 8


def test_cooldown_uses_injected_clock():
    now = {"t": 1000.0}
    limiter = AdaptiveLimiter(max_concurrency=8, cooldown_seconds=60.0, clock=lambda: now["t"])
    limiter.record_rate_limit()
    assert limiter.in_cooldown() is True
    now["t"] += 59.0
    assert limiter.in_cooldown() is True
    now["t"] += 2.0
    assert limiter.in_cooldown() is False


def test_slot_tracks_inflight_and_allows_entering_after_cooldown():
    now = {"t": 0.0}
    limiter = AdaptiveLimiter(max_concurrency=2, cooldown_seconds=10.0, clock=lambda: now["t"])
    limiter.record_rate_limit()
    now["t"] = 11.0
    with limiter.slot(timeout=0.5):
        assert limiter.stats()["inflight"] == 1
    assert limiter.stats()["inflight"] == 0


def test_slot_raises_timeout_when_cooldown_never_ends():
    limiter = AdaptiveLimiter(max_concurrency=1, cooldown_seconds=600.0)
    limiter.record_rate_limit()
    with pytest.raises(TimeoutError):
        with limiter.slot(timeout=0.05):
            pass


def test_transport_error_pauses_without_halving_until_repeated():
    """单次断连只是链路抖动：停顿一下，不降并发；连续 3 次才升级成硬限流。"""
    limiter = AdaptiveLimiter(
        max_concurrency=8, cooldown_seconds=60.0, soft_cooldown_seconds=5.0, soft_escalate_after=3
    )
    assert limiter.record_transport_error() == 8
    assert limiter.limit == 8
    assert 0 < limiter.stats()["cooldown_remaining"] <= 5

    assert limiter.record_transport_error() == 8
    assert limiter.limit == 8

    assert limiter.record_transport_error() == 4  # 第 3 次连续失败 → 当作真限流，减半
    assert limiter.stats()["cooldown_remaining"] > 5  # 用完整冷却，不是 5 秒


def test_success_resets_transport_error_streak():
    limiter = AdaptiveLimiter(max_concurrency=8, soft_escalate_after=3)
    limiter.record_transport_error()
    limiter.record_transport_error()
    limiter.record_success()
    assert limiter.stats()["soft_failures"] == 0
    assert limiter.record_transport_error() == 8  # 成功过就不算"连续失败"，不会一上来就升级
