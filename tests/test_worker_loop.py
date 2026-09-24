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
