import threading
import time

from audiobook import jobs
from audiobook.worker import HANDLERS, WorkerContext, register, run_forever, run_once


def make_ctx(conn, settings, engine=None) -> WorkerContext:
    return WorkerContext(settings=settings, conn=conn, worker_id="w-test", engine=engine)


def test_run_once_returns_false_when_queue_empty(conn, settings):
    assert run_once(make_ctx(conn, settings)) is False


def test_run_once_executes_registered_handler(conn, settings):
    seen = {}

    @register("unit_kind")
    def _handler(ctx, job):
        seen["job_id"] = job.id
        ctx.progress(job, 1, 1, "ok")

    try:
        job_id = jobs.enqueue(conn, "unit_kind", "book1", 1)
        assert run_once(make_ctx(conn, settings)) is True
        assert seen["job_id"] == job_id
        job = jobs.get_job(conn, job_id)
        assert job.status == "done"
        assert job.progress == {"done": 1, "total": 1, "message": "ok"}
    finally:
        HANDLERS.pop("unit_kind", None)


def test_failing_handler_marks_failed_when_attempts_exhausted(conn, settings):
    @register("unit_fail")
    def _handler(ctx, job):
        raise RuntimeError("boom")

    try:
        job_id = jobs.enqueue(conn, "unit_fail", "book1", 1, max_attempts=1)
        run_once(make_ctx(conn, settings))
        job = jobs.get_job(conn, job_id)
        assert job.status == "failed"
    finally:
        HANDLERS.pop("unit_fail", None)


def test_unknown_kind_fails_job(conn, settings):
    job_id = jobs.enqueue(conn, "no_such_kind", "book1", 1, max_attempts=1)
    run_once(make_ctx(conn, settings))
    assert jobs.get_job(conn, job_id).status == "failed"


def test_handler_can_stop_when_user_cancels(conn, settings):
    """handler 察觉取消后抛 JobCancelled：落成 canceled，不算失败、不重排。"""

    @register("unit_cancel")
    def _handler(ctx, job):
        jobs.request_cancel(ctx.conn, job.id)
        ctx.raise_if_cancelled(job)

    try:
        job_id = jobs.enqueue(conn, "unit_cancel", "book1", 1, max_attempts=3)
        assert run_once(make_ctx(conn, settings)) is True
        job = jobs.get_job(conn, job_id)
        assert job.status == "canceled"
        assert job.attempts == 1
        assert jobs.claim(conn, "w-other") is None  # 不会重新排队
    finally:
        HANDLERS.pop("unit_cancel", None)


def test_cancel_requested_mid_run_is_not_marked_done(conn, settings):
    """handler 正常跑完、但期间用户点过取消：以取消为准，别标成已完成。"""

    @register("unit_cancel_late")
    def _handler(ctx, job):
        jobs.request_cancel(ctx.conn, job.id)

    try:
        job_id = jobs.enqueue(conn, "unit_cancel_late", "book1", 1)
        assert run_once(make_ctx(conn, settings)) is True
        assert jobs.get_job(conn, job_id).status == "canceled"
    finally:
        HANDLERS.pop("unit_cancel_late", None)


def test_long_handler_keeps_lease_alive(conn, settings):
    @register("unit_slow")
    def _handler(ctx, job):
        time.sleep(2.0)

    try:
        job_id = jobs.enqueue(conn, "unit_slow", "book1", 1)
        run_once(make_ctx(conn, settings), lease_seconds=1)
        assert jobs.get_job(conn, job_id).status == "done"
        assert jobs.reap_expired(conn) == 0
    finally:
        HANDLERS.pop("unit_slow", None)


def test_refresh_rebuilds_engine_when_endpoints_change(conn, settings):
    """改端点/并发不用重启 worker：下一轮任务前自动换引擎。"""
    built = []
    # 起点显式给成"没配端点"：.env 里可能已经写了端点，别让用例依赖本机配置
    settings = settings.model_copy(update={"engine": "http", "tts_endpoints": []})

    def factory(fresh):
        built.append(fresh.engine)
        return {"engine": fresh.engine}

    ctx = WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w-test",
        engine={"engine": "fake"},
        reload_settings=lambda: settings.model_copy(update={"engine": "http", "tts_endpoints": ["http://127.0.0.1:8020"]}),
        engine_factory=factory,
    )
    assert ctx.refresh() is True
    assert built == ["http"]
    assert ctx.engine == {"engine": "http"}
    assert ctx.settings.tts_endpoints == ["http://127.0.0.1:8020"]
    # 设置没再变就不用反复重建
    assert ctx.refresh() is False
    assert built == ["http"]


def test_refresh_keeps_old_engine_when_rebuild_fails(conn, settings):
    def broken(_fresh):
        raise RuntimeError("TTS 端点还没起来")

    ctx = WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w-test",
        engine={"engine": "fake"},
        reload_settings=lambda: settings.model_copy(update={"engine": "http", "tts_endpoints": ["http://127.0.0.1:9"]}),
        engine_factory=broken,
    )
    assert ctx.refresh() is True
    assert ctx.engine == {"engine": "fake"}


def test_run_once_reloads_settings_before_claiming(conn, settings):
    calls = []
    ctx = WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w-test",
        reload_settings=lambda: (calls.append(1), settings)[1],
    )
    assert run_once(ctx) is False
    assert calls == [1]


def _parallel_probe():
    """记录同时在跑的任务数（全局 + 按书），给下面的并发用例用。"""
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0, "books": {}, "book_peak": 0}

    def enter(book_id: str) -> None:
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            state["books"][book_id] = state["books"].get(book_id, 0) + 1
            state["book_peak"] = max(state["book_peak"], state["books"][book_id])

    def leave(book_id: str) -> None:
        with lock:
            state["inflight"] -= 1
            state["books"][book_id] -= 1

    return state, lock, enter, leave


def test_run_forever_runs_different_books_in_parallel(conn, settings):
    state, _lock, enter, leave = _parallel_probe()

    @register("unit_parallel")
    def _handler(ctx, job):
        enter(job.book_id)
        try:
            time.sleep(0.3)
        finally:
            leave(job.book_id)

    try:
        jobs.enqueue(conn, "unit_parallel", "b1", 1)
        jobs.enqueue(conn, "unit_parallel", "b2", 1)
        ctx = WorkerContext(
            settings=settings.model_copy(update={"worker_concurrency": 2}),
            conn=conn,
            worker_id="w-test",
        )
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["peak"] >= 2          # 两本书真的同时在跑
        assert state["book_peak"] == 1     # 每本书自己只有一个任务
    finally:
        HANDLERS.pop("unit_parallel", None)


def test_run_forever_serializes_jobs_of_the_same_book(conn, settings):
    state, _lock, enter, leave = _parallel_probe()

    @register("unit_same_book")
    def _handler(ctx, job):
        enter(job.book_id)
        try:
            time.sleep(0.2)
        finally:
            leave(job.book_id)

    try:
        jobs.enqueue(conn, "unit_same_book", "b1", 1)
        jobs.enqueue(conn, "unit_same_book", "b1", 2)
        ctx = WorkerContext(
            settings=settings.model_copy(update={"worker_concurrency": 2}),
            conn=conn,
            worker_id="w-test",
        )
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["book_peak"] == 1     # 同一本书的两个任务不会重叠
    finally:
        HANDLERS.pop("unit_same_book", None)
