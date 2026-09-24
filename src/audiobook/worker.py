import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from . import jobs
from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class WorkerContext:
    settings: Settings
    conn: object
    worker_id: str
    engine: object | None = None

    def progress(self, job, done: int, total: int, message: str = "") -> None:
        jobs.set_progress(self.conn, job.id, done, total, message)


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
