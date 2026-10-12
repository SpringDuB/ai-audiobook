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

# ---------------------------------------------------------------- 任务分型（并发策略）
# 并发策略只看 kind，不写进 jobs.claim：claim 只认过滤条件，策略集中在这里。
#
# 1) GPU_KINDS：吃 TTS/显存的长任务（整章合成），全局最多 worker_tts_jobs 条（默认 1）。
#    试听（voice_preview）和单行重合成（synthesize_line）刻意不进来 —— 它们是"条级"
#    短任务，用户等不起，允许插在整章合成旁边跑（TTS 服务端自己还有请求闸门兜底，
#    插进去只是排队，不会把显存挤爆）。
GPU_KINDS = frozenset({"synthesize"})
# 2) ANALYSIS_CHAIN_KINDS：分析链会读改写同一份 characters.json / casting.json，
#    同一本书里同时只跑一条，避免并发读改写互相覆盖（不同书不受影响）。
ANALYSIS_CHAIN_KINDS = frozenset(
    {"chapter_split", "characters", "chapters", "lines", "casting", "voice_design"}
)
# 3) CHAPTER_CLIP_KINDS：碰同一章音轨 / 成品的任务（合成、单行重合成、渲染、重落行），
#    同一本书同一章同时只跑一条 —— 渲染读到合成到一半的片段会产出残缺成品。
CHAPTER_CLIP_KINDS = frozenset({"synthesize", "synthesize_line", "post", "lines"})


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


def run_job(ctx: WorkerContext, job, *, lease_seconds: int | None = None) -> None:
    """跑一个已领取的任务：心跳 + handler + 落库（完成 / 取消 / 失败）。"""
    lease = lease_seconds or ctx.settings.lease_seconds
    ctx.watcher = CancelWatcher(ctx.settings.db_path, job.id)
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(ctx, job.id, lease, stop), daemon=True)
    hb.start()
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
        stop.set()
        hb.join(timeout=1.0)
        ctx.watcher.close()
        ctx.watcher = None


def run_once(ctx: WorkerContext, lease_seconds: int | None = None) -> bool:
    ctx.refresh()
    lease = lease_seconds or ctx.settings.lease_seconds
    jobs.reap_expired(ctx.conn)
    job = jobs.claim(
        ctx.conn,
        ctx.worker_id,
        lease,
        exclusive_per_book=ANALYSIS_CHAIN_KINDS,
        exclusive_per_chapter=CHAPTER_CLIP_KINDS,
    )
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
    """worker 主循环：任务按"吃不吃 GPU"分两条通道并行。

    并发规则（要的就是"生成整本音频时别的操作照常能跑"）：

    - 整章合成（``GPU_KINDS``）全局最多 ``worker_tts_jobs`` 条（默认 1）：显存就一份，
      两个整章任务并排只会互相抢。上限写在 claim 的 SQL 里，多开 worker 也拦得住。
    - 其余任务（分析、选角、音色描述、渲染、导出、试听、单行重合成）最多
      ``worker_concurrency`` 条并行，且与整章合成互不排队 —— 整本合成在跑的时候，
      点试听、重渲染某一章、重新分析都照常执行。
    - 同一本书的分析链串行、同一章的音轨链串行（见 ``jobs.claim`` 的
      ``exclusive_per_book`` / ``exclusive_per_chapter``）。
    - 只有全部槽位空闲时才重读设置：重建引擎 / LLM 客户端不能打断在跑的任务。
    """
    interval = poll_seconds or ctx.settings.worker_poll_seconds
    gpu_slots = max(1, int(getattr(ctx.settings, "worker_tts_jobs", 1) or 1))
    other_slots = max(1, int(getattr(ctx.settings, "worker_concurrency", 1) or 1))
    executed = 0
    running: dict = {}

    def work(job) -> None:
        # 每个任务一条 sqlite 连接：跨线程共用一条连接会把事务/BEGIN 交错在一起
        conn = connect(ctx.settings.db_path)
        try:
            job_ctx = replace(ctx, conn=conn)
            run_job(job_ctx, job, lease_seconds=ctx.settings.lease_seconds)
        finally:
            conn.close()

    def claim_for_lane(gpu_free: int, other_free: int):
        """按通道领任务：GPU 通道有空位先领整章合成，再补其它任务。"""
        if gpu_free > 0:
            job = jobs.claim(
                ctx.conn,
                ctx.worker_id,
                ctx.settings.lease_seconds,
                kinds=GPU_KINDS,
                max_running=(GPU_KINDS, gpu_slots),
                exclusive_per_book=ANALYSIS_CHAIN_KINDS,
                exclusive_per_chapter=CHAPTER_CLIP_KINDS,
            )
            if job is not None:
                return job
        if other_free > 0:
            return jobs.claim(
                ctx.conn,
                ctx.worker_id,
                ctx.settings.lease_seconds,
                exclude_kinds=GPU_KINDS,
                exclusive_per_book=ANALYSIS_CHAIN_KINDS,
                exclusive_per_chapter=CHAPTER_CLIP_KINDS,
            )
        return None

    with ThreadPoolExecutor(max_workers=gpu_slots + other_slots, thread_name_prefix="job") as pool:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            # 回收死掉的 worker 留下的租约（含"点过取消"的：直接落成 canceled）
            try:
                jobs.reap_expired(ctx.conn)
            except Exception as exc:  # noqa: BLE001
                logger.warning("回收过期租约失败：%s", exc)
            if not running:
                ctx.refresh()
            while True:
                gpu_n = sum(1 for kind, _job_id in running.values() if kind in GPU_KINDS)
                other_n = len(running) - gpu_n
                if gpu_n >= gpu_slots and other_n >= other_slots:
                    break
                try:
                    job = claim_for_lane(gpu_slots - gpu_n, other_slots - other_n)
                except Exception as exc:  # noqa: BLE001 - 领任务偶发失败不该让 worker 退出
                    logger.warning("领取任务失败：%s", exc)
                    break
                if job is None:
                    break
                running[pool.submit(work, job)] = (job.kind, job.id)
            if running:
                done, _ = wait(list(running), timeout=interval, return_when=FIRST_COMPLETED)
                for future in done:
                    entry = running.pop(future, None)
                    job_id = entry[1] if entry else None
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
