import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from . import jobs
from .config import Settings
from .db import connect

logger = logging.getLogger(__name__)

class JobCancelled(Exception):
    """handler 察觉到用户在任务界面点了取消：任务落成 canceled，不算失败、不重试。"""


class CancelWatcher:
    """任务的取消检查：自带一条 sqlite 连接 + 锁，可安全地在任务的任意线程里调用。

    worker 给每个任务单独开一条 conn（跨线程共用事务会交错），而整章提取、音色描述
    这类任务本身是线程池并发跑的 —— 取消检查会从那些线程里发起，所以这里再开一条
    只读连接专门查 cancel_requested。取消要在几秒内打断正在跑的 LLM 请求，
    不能等整个任务自己跑完。
    """

    def __init__(self, db_path, job_id: int):
        self.job_id = int(job_id)
        self._conn = connect(db_path)
        self._lock = threading.Lock()

    def requested(self) -> bool:
        with self._lock:
            if self._conn is None:
                return False
            return jobs.is_canceled(self._conn, self.job_id)

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


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
    "llm_stream",
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
    # 每个任务自己的取消检查（run_job 里装上），跨线程可用
    watcher: "CancelWatcher | None" = None

    def progress(self, job, done: int, total: int, message: str = "", extra: dict | None = None) -> None:
        jobs.set_progress(self.conn, job.id, done, total, message, extra=extra)

    def cancelled(self, job) -> bool:
        """任务是不是被点了取消（队列里的取消会直接把状态改成 canceled）。"""
        watcher = self.watcher
        if watcher is not None and watcher.job_id == job.id:
            return watcher.requested()
        return jobs.is_canceled(self.conn, job.id)

    def raise_if_cancelled(self, job) -> None:
        if self.cancelled(job):
            raise JobCancelled(f"任务 {job.id} 已取消")

    def cancel_check(self, job) -> Callable[[], None]:
        """给 LLM 层用的取消回调：取消时抛 JobCancelled。

        返回的函数可以安全地在任务的线程池里调用（走 CancelWatcher 的专用连接）。
        """

        def check() -> None:
            self.raise_if_cancelled(job)

        return check

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
    # 心跳线程自己一条连接：不跟正在跑 handler 的那条连接抢事务
    conn = connect(ctx.settings.db_path)
    try:
        while not stop.wait(interval):
            try:
                alive = jobs.heartbeat(conn, job_id, ctx.worker_id, lease_seconds)
            except Exception as exc:  # noqa: BLE001 - 一次心跳失败不致命
                logger.warning("任务 %s 心跳异常：%s", job_id, exc)
                continue
            if not alive:
                logger.warning("心跳失败，任务 %s 可能已被回收", job_id)
                return
    finally:
        conn.close()


def run_job(ctx: WorkerContext, job, *, lease_seconds: int | None = None, gate=None) -> None:
    """跑一个已领取的任务：心跳 + handler + 落库（完成 / 取消 / 失败）。

    ``gate`` 是可选的并发闸门（比如吃 GPU 的合成任务），拿不到就排队等着，
    期间心跳继续续租，任务不会被别的 worker 抢走。
    """
    lease = lease_seconds or ctx.settings.lease_seconds
    ctx.watcher = CancelWatcher(ctx.settings.db_path, job.id)
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(ctx, job.id, lease, stop), daemon=True)
    hb.start()
    if gate is not None:
        gate.acquire()
    try:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            raise RuntimeError(f"未注册的任务类型: {job.kind}")
        handler(ctx, job)
    except JobCancelled as exc:
        logger.info("任务 %s 已取消：%s", job.id, exc)
        jobs.mark_canceled(ctx.conn, job.id)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库，不能让 worker 退出
        logger.exception("任务 %s 执行失败", job.id)
        jobs.fail(ctx.conn, job.id, ctx.worker_id, f"{type(exc).__name__}: {exc}")
    else:
        # handler 正常跑完，但期间用户点过取消：以取消为准，别标成已完成
        if jobs.is_canceled(ctx.conn, job.id):
            logger.info("任务 %s 执行完时发现取消请求，按取消处理", job.id)
            jobs.mark_canceled(ctx.conn, job.id)
        else:
            jobs.complete(ctx.conn, job.id, ctx.worker_id)
    finally:
        if gate is not None:
            gate.release()
        stop.set()
        hb.join(timeout=1.0)
        ctx.watcher.close()
        ctx.watcher = None


def run_once(ctx: WorkerContext, lease_seconds: int | None = None) -> bool:
    ctx.refresh()
    lease = lease_seconds or ctx.settings.lease_seconds
    jobs.reap_expired(ctx.conn)
    job = jobs.claim(ctx.conn, ctx.worker_id, lease)
    if job is None:
        return False
    run_job(ctx, job, lease_seconds=lease)
    return True


def run_forever(
    ctx: WorkerContext,
    poll_seconds: float | None = None,
    stop_event=None,
    max_jobs: int | None = None,
) -> int:
    """worker 主循环：同时跑 ``worker_concurrency`` 个任务。

    并发规则：
    - 不同书的任务可以并行（各占一个槽位）；
    - 同一本书同时只跑一个任务（jobs.claim 里按 book_id 排他），保证
      分析 → 选角 → 合成 → 渲染 → 合本 的依赖顺序；
    - ``worker_tts_jobs`` 控制"吃 TTS/GPU"的合成任务同时跑几个，默认 1，
      免得两个任务互相抢显存；
    - 只有全部槽位空闲时才重读设置：重建引擎 / LLM 客户端不能打断在跑的任务。
    """
    interval = poll_seconds or ctx.settings.worker_poll_seconds
    slots = max(1, int(getattr(ctx.settings, "worker_concurrency", 1) or 1))
    tts_jobs = max(1, int(getattr(ctx.settings, "worker_tts_jobs", 1) or 1))
    tts_gate = threading.Semaphore(tts_jobs)
    executed = 0
    running: dict = {}

    def work(job) -> None:
        # 每个任务一条 sqlite 连接：跨线程共用一条连接会把事务/BEGIN 交错在一起
        conn = connect(ctx.settings.db_path)
        try:
            job_ctx = replace(ctx, conn=conn)
            gate = tts_gate if job.kind == "synthesize" else None
            run_job(job_ctx, job, lease_seconds=ctx.settings.lease_seconds, gate=gate)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="job") as pool:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            # 回收死掉的 worker 留下的租约（含"点过取消"的：直接落成 canceled）
            try:
                jobs.reap_expired(ctx.conn)
            except Exception as exc:  # noqa: BLE001
                logger.warning("回收过期租约失败：%s", exc)
            while len(running) < slots:
                if not running:
                    ctx.refresh()
                try:
                    job = jobs.claim(ctx.conn, ctx.worker_id, ctx.settings.lease_seconds)
                except Exception as exc:  # noqa: BLE001 - 领任务偶发失败不该让 worker 退出
                    logger.warning("领取任务失败：%s", exc)
                    break
                if job is None:
                    break
                running[pool.submit(work, job)] = job.id
            if running:
                done, _ = wait(list(running), timeout=interval, return_when=FIRST_COMPLETED)
                for future in done:
                    job_id = running.pop(future, None)
                    executed += 1
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 - 线程里崩了也要能继续跑下一个
                        logger.exception("任务 %s 的执行线程异常：%s", job_id, exc)
                if max_jobs is not None and executed >= max_jobs:
                    break
                continue
            if max_jobs is not None:
                break
            time.sleep(interval)
    return executed
