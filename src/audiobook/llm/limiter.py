import threading
import time
from contextlib import contextmanager
from typing import Callable


class AdaptiveLimiter:
    """自适应并发门：限流/超时降一档并冷却，连续成功后逐级恢复。"""

    def __init__(
        self,
        max_concurrency: int = 8,
        min_concurrency: int = 1,
        restore_after: int = 8,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_concurrency = max(1, int(min_concurrency))
        self.restore_after = max(1, int(restore_after))
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._limit = self.max_concurrency
        self._inflight = 0
        self._cooldown_until = 0.0
        self._success_streak = 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    def stats(self) -> dict:
        with self._cond:
            return {
                "limit": self._limit,
                "inflight": self._inflight,
                "cooldown_remaining": max(0.0, self._cooldown_until - self._clock()),
                "success_streak": self._success_streak,
            }

    def in_cooldown(self) -> bool:
        return self.stats()["cooldown_remaining"] > 0

    def record_rate_limit(self) -> int:
        with self._cond:
            self._limit = max(self.min_concurrency, self._limit // 2)
            self._cooldown_until = self._clock() + self.cooldown_seconds
            self._success_streak = 0
            self._cond.notify_all()
            return self._limit

    def record_success(self) -> int:
        with self._cond:
            self._success_streak += 1
            if self._success_streak >= self.restore_after and self._limit < self.max_concurrency:
                self._limit += 1
                self._success_streak = 0
                self._cond.notify_all()
            return self._limit

    @contextmanager
    def slot(self, timeout: float | None = None):
        self._acquire(timeout)
        try:
            yield
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def _acquire(self, timeout: float | None) -> None:
        deadline = None if timeout is None else self._clock() + timeout
        with self._cond:
            while True:
                wait = self._cooldown_until - self._clock()
                if wait <= 0 and self._inflight < self._limit:
                    self._inflight += 1
                    return
                if wait <= 0:
                    wait = 0.05
                if deadline is not None and self._clock() + wait > deadline:
                    raise TimeoutError("等待 LLM 并发槽超时")
                self._cond.wait(timeout=wait)
