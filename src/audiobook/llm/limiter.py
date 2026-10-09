"""固定并发门：并发上限只由配置决定，请求失败一律只重试，不自动降档。

历史行为会在失败时"减半 + 冷却"自适应降档，真实批量跑下来被证明有害：
服务端一次网络抖动会同时掐断多路连接，几毫秒内就吃满"连续失败"阈值，
10 路并发被打到 1 路；而恢复要每 8 次成功才 +1，对长任务等于永久降速。

现在：
- 断连 / 超时：不动并发，交给 runner 的抖动退避 + 重试；
- 429（服务端明确限流）：同样不动并发，重试自带退避，不降用户的档；
- ``stats()`` 里的计数只用于观测（日志/排查），不参与调度。
"""

import threading
import time
from contextlib import contextmanager
from typing import Callable


class AdaptiveLimiter:
    """（名字保留兼容调用方）现在的行为是固定并发门。"""

    def __init__(
        self,
        max_concurrency: int = 8,
        min_concurrency: int = 1,
        clock: Callable[[], float] = time.monotonic,
        **legacy,
    ):
        # legacy 参数（cooldown_seconds / restore_after / soft_*）只为兼容老调用方，
        # 已经不再影响调度：降档逻辑整体删掉了。
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_concurrency = max(1, int(min_concurrency))
        self._clock = clock
        self._limit = self.max_concurrency
        self._inflight = 0
        self._transport_failures = 0
        self._rate_limits = 0
        self._successes = 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def stats(self) -> dict:
        with self._cond:
            return {
                "limit": self._limit,
                "inflight": self._inflight,
                "transport_failures": self._transport_failures,
                "rate_limits": self._rate_limits,
                "successes": self._successes,
            }

    def in_cooldown(self) -> bool:
        """固定并发门没有冷却期（保留接口给老调用方）。"""
        return False

    def record_rate_limit(self) -> int:
        """429：只计数。降档在这里被明确禁止——失败靠重试，不靠降并发。"""
        with self._cond:
            self._rate_limits += 1
            return self._limit

    def record_transport_error(self) -> int:
        """断连 / 超时：只计数，不降档、不冻结连接池，并发原样保持。"""
        with self._cond:
            self._transport_failures += 1
            return self._limit

    def record_success(self) -> int:
        with self._cond:
            self._successes += 1
            return self._limit

    @contextmanager
    def slot(self, timeout: float | None = None, cancel_check: Callable[[], None] | None = None):
        """占一个并发槽；``cancel_check`` 会在等待期间反复调用，用户取消时立刻抛错退出。"""
        self._acquire(timeout, cancel_check)
        try:
            yield
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def _acquire(self, timeout: float | None, cancel_check: Callable[[], None] | None = None) -> None:
        deadline = None if timeout is None else self._clock() + timeout
        with self._cond:
            while True:
                if cancel_check is not None:
                    # 等槽可能等很久（并发打满时）：取消要在这里就能打断
                    cancel_check()
                if self._inflight < self._limit:
                    self._inflight += 1
                    return
                if deadline is not None and self._clock() + 0.05 > deadline:
                    raise TimeoutError("等待 LLM 并发槽超时")
                self._cond.wait(timeout=0.05)
