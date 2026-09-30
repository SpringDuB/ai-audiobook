"""并发门：请求失败只重试，永远不降档（用户明确要求：不要降低并发）。"""

import threading
import time

import pytest

from audiobook.llm.limiter import AdaptiveLimiter


def test_failures_never_lower_the_limit():
    limiter = AdaptiveLimiter(max_concurrency=10)
    assert limiter.limit == 10
    for _ in range(30):
        assert limiter.record_transport_error() == 10
    for _ in range(30):
        assert limiter.record_rate_limit() == 10
    assert limiter.limit == 10
    stats = limiter.stats()
    assert stats["transport_failures"] == 30
    assert stats["rate_limits"] == 30


def test_slot_tracks_inflight():
    limiter = AdaptiveLimiter(max_concurrency=2)
    with limiter.slot(timeout=0.5):
        assert limiter.stats()["inflight"] == 1
    assert limiter.stats()["inflight"] == 0


def test_slot_times_out_when_no_slot_frees_up():
    limiter = AdaptiveLimiter(max_concurrency=1)
    with limiter.slot():
        with pytest.raises(TimeoutError):
            with limiter.slot(timeout=0.05):
                pass


def test_legacy_downgrade_knobs_are_ignored():
    """老调用方还在传 cooldown_seconds / soft_escalate_after：一律不再影响并发档位。"""
    limiter = AdaptiveLimiter(
        max_concurrency=4,
        cooldown_seconds=60.0,
        soft_escalate_after=3,
        soft_cooldown_seconds=5.0,
        restore_after=1,
    )
    for _ in range(9):
        limiter.record_transport_error()
    limiter.record_rate_limit()
    assert limiter.limit == 4
    limiter.record_success()
    assert limiter.limit == 4
    assert limiter.in_cooldown() is False


def test_slots_cap_real_concurrency():
    limiter = AdaptiveLimiter(max_concurrency=3)
    lock = threading.Lock()
    start = threading.Barrier(12)
    state = {"inflight": 0, "peak": 0}

    def work():
        start.wait()
        with limiter.slot(timeout=5):
            with lock:
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
            time.sleep(0.02)
            with lock:
                state["inflight"] -= 1

    threads = [threading.Thread(target=work) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["peak"] == 3
