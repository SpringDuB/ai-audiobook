import threading
import time

import pytest

from audiobook import worker as worker_mod
from audiobook import jobs
from audiobook.worker import (
    HANDLERS,
    CancelWatcher,
    JobCancelled,
    WorkerContext,
    register,
    run_forever,
    run_once,
)


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


def test_cancel_check_works_from_any_thread(conn, settings):
    """取消检查要能在任务的线程池里调用：CancelWatcher 自带连接 + 锁。"""
    job_id = jobs.enqueue(conn, "lines", "book1", 1)
    job = jobs.claim(conn, "w-test", lease_seconds=30)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w-test")
    ctx.watcher = CancelWatcher(settings.db_path, job.id)
    try:
        check = ctx.cancel_check(job)
        check()      # 还没取消：不抛
        jobs.request_cancel(conn, job.id)
        errors: list[BaseException] = []

        def run():
            try:
                check()
            except BaseException as exc:  # noqa: BLE001 - 线程里收集异常
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert isinstance(errors[0] if errors else None, JobCancelled)
        assert jobs.get_job(conn, job_id).cancel_requested is True
    finally:
        ctx.watcher.close()


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


def test_run_forever_runs_same_book_jobs_in_parallel(conn, settings):
    """同一本书里互不相关的任务现在能并行（这是"生成整本音频时别的照常跑"的基础）。"""
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
        assert state["book_peak"] == 2     # 同一本书的两个任务真在并行
    finally:
        HANDLERS.pop("unit_same_book", None)


def test_run_forever_keeps_other_tasks_running_during_gpu_synth(conn, settings, monkeypatch):
    """整章合成（GPU 通道）跑的时候，渲染 / 试听这类任务照常并行；合成自己仍是 1 条。"""
    monkeypatch.setattr(worker_mod, "GPU_KINDS", frozenset({"unit_gpu_synth"}))
    lock = threading.Lock()
    state = {"total": 0, "peak": 0, "gpu": 0, "gpu_peak": 0, "overlap": False}

    @register("unit_gpu_synth")
    def _synth(ctx, job):
        with lock:
            state["total"] += 1
            state["gpu"] += 1
            state["peak"] = max(state["peak"], state["total"])
            state["gpu_peak"] = max(state["gpu_peak"], state["gpu"])
        try:
            time.sleep(0.6)
        finally:
            with lock:
                state["gpu"] -= 1
                state["total"] -= 1

    @register("unit_render")
    def _render(ctx, job):
        with lock:
            state["total"] += 1
            state["peak"] = max(state["peak"], state["total"])
        try:
            deadline = time.time() + 3
            while time.time() < deadline:
                with lock:
                    if state["gpu"] > 0:
                        state["overlap"] = True
                        break
                time.sleep(0.01)
            time.sleep(0.2)
        finally:
            with lock:
                state["total"] -= 1

    try:
        jobs.enqueue(conn, "unit_gpu_synth", "b1", 1)
        jobs.enqueue(conn, "unit_gpu_synth", "b2", 1)   # 第二条合成必须等第一条跑完
        jobs.enqueue(conn, "unit_render", "b1", 2)      # 同书另一章：要能和合成并行
        ctx = WorkerContext(
            settings=settings.model_copy(update={"worker_concurrency": 2, "worker_tts_jobs": 1}),
            conn=conn,
            worker_id="w-test",
        )
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=3) == 3
        assert state["overlap"] is True    # 合成没跑完，渲染已经开工
        assert state["gpu_peak"] == 1      # 整章合成全局只有 1 条
        assert state["peak"] >= 2          # 两条通道确实同时在跑
    finally:
        HANDLERS.pop("unit_gpu_synth", None)
        HANDLERS.pop("unit_render", None)


def test_run_forever_serializes_same_chapter_clip_work(conn, settings, monkeypatch):
    """同一章的音轨链（合成 / 渲染）互斥：防止渲染读到合成到一半的片段。"""
    monkeypatch.setattr(
        worker_mod, "CHAPTER_CLIP_KINDS", frozenset({"unit_clip_a", "unit_clip_b"})
    )
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def handler(ctx, job):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            time.sleep(0.25)
        finally:
            with lock:
                state["inflight"] -= 1

    register("unit_clip_a")(handler)
    register("unit_clip_b")(handler)
    ctx = WorkerContext(
        settings=settings.model_copy(update={"worker_concurrency": 2, "worker_tts_jobs": 1}),
        conn=conn,
        worker_id="w-test",
    )
    try:
        jobs.enqueue(conn, "unit_clip_a", "b1", 1)
        jobs.enqueue(conn, "unit_clip_b", "b1", 1)
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["peak"] == 1          # 同一章：一条接一条

        state["peak"] = 0
        jobs.enqueue(conn, "unit_clip_a", "b1", 1)
        jobs.enqueue(conn, "unit_clip_b", "b1", 2)
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["peak"] == 2          # 不同章：并行跑
    finally:
        HANDLERS.pop("unit_clip_a", None)
        HANDLERS.pop("unit_clip_b", None)


def test_run_forever_serializes_analysis_chain_per_book(conn, settings, monkeypatch):
    """分析链同一本书串行（characters.json / casting.json 并发读改写会互相覆盖）。"""
    monkeypatch.setattr(
        worker_mod, "ANALYSIS_CHAIN_KINDS", frozenset({"unit_chain_a", "unit_chain_b"})
    )
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def handler(ctx, job):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            time.sleep(0.25)
        finally:
            with lock:
                state["inflight"] -= 1

    register("unit_chain_a")(handler)
    register("unit_chain_b")(handler)
    ctx = WorkerContext(
        settings=settings.model_copy(update={"worker_concurrency": 2, "worker_tts_jobs": 1}),
        conn=conn,
        worker_id="w-test",
    )
    try:
        jobs.enqueue(conn, "unit_chain_a", "b1", 1)
        jobs.enqueue(conn, "unit_chain_b", "b1", 2)
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["peak"] == 1          # 同一本书：一条接一条

        state["peak"] = 0
        jobs.enqueue(conn, "unit_chain_a", "b1", 1)
        jobs.enqueue(conn, "unit_chain_b", "b2", 1)
        assert run_forever(ctx, poll_seconds=0.05, max_jobs=2) == 2
        assert state["peak"] == 2          # 不同书：并行跑
    finally:
        HANDLERS.pop("unit_chain_a", None)
        HANDLERS.pop("unit_chain_b", None)
