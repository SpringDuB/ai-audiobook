import time

from audiobook import jobs
from audiobook.worker import HANDLERS, WorkerContext, register, run_once


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
