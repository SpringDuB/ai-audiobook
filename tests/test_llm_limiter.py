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
