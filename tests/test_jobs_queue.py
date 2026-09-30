from audiobook import jobs


def test_enqueue_is_idempotent_for_active_job(conn):
    first = jobs.enqueue(conn, "synthesize", "book1", 1)
    second = jobs.enqueue(conn, "synthesize", "book1", 1)
    assert first == second


def test_failed_job_can_be_retried(conn):
    job_id = jobs.enqueue(conn, "post", "b1", 1)
    for _ in range(3):
        job = jobs.claim(conn, "w1")
        jobs.fail(conn, job.id, "w1", "boom", retry_delay_ms=0)
    assert jobs.get_job(conn, job_id).status == "failed"

    assert jobs.retry(conn, job_id) is True
    fresh = jobs.get_job(conn, job_id)
    assert (fresh.status, fresh.attempts, fresh.error) == ("queued", 0, None)
    assert jobs.retry(conn, job_id) is False


def test_job_carries_timestamps_and_error(conn):
    job_id = jobs.enqueue(conn, "post", "b1", 1, now=1000)
    job = jobs.get_job(conn, job_id)
    assert (job.created_at, job.updated_at, job.error) == (1000, 1000, None)


def test_enqueue_line_records_target_line(conn):
    job_id = jobs.enqueue_line(conn, "b1", 2, "c0002-s01-l005")
    job = jobs.get_job(conn, job_id)
    assert (job.kind, job.chapter_index) == ("synthesize_line", 2)
    assert job.progress == {"pending_line": "c0002-s01-l005"}


def test_enqueue_allows_new_job_after_done(conn):
    first = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.claim(conn, "w1")
    jobs.complete(conn, first, "w1")
    second = jobs.enqueue(conn, "synthesize", "book1", 1)
    assert second != first


def test_claim_marks_running_and_counts_attempt(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1)
    claimed = jobs.claim(conn, "w1")
    assert claimed.id == job_id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert jobs.claim(conn, "w2") is None


def test_claim_respects_not_before(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.fail(conn, job_id, "w1", "boom", retry_delay_ms=60_000, now=1_000)
    assert jobs.claim(conn, "w2", now=2_000) is None
    assert jobs.claim(conn, "w2", now=61_001).id == job_id


def test_fail_after_max_attempts_marks_failed(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1, max_attempts=2)
    jobs.claim(conn, "w1")
    assert jobs.fail(conn, job_id, "w1", "e1", retry_delay_ms=0) == "queued"
    jobs.claim(conn, "w1")
    assert jobs.fail(conn, job_id, "w1", "e2", retry_delay_ms=0) == "failed"
    assert jobs.get_job(conn, job_id).status == "failed"


def test_reap_expired_requeues_dead_worker(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.claim(conn, "w1", lease_seconds=10, now=1_000)
    assert jobs.reap_expired(conn, now=5_000) == 0
    assert jobs.reap_expired(conn, now=20_000) == 1
    assert jobs.get_job(conn, job_id).status == "queued"


def test_heartbeat_extends_lease(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.claim(conn, "w1", lease_seconds=10, now=1_000)
    assert jobs.heartbeat(conn, job_id, "w1", lease_seconds=10, now=9_000) is True
    assert jobs.reap_expired(conn, now=15_000) == 0
    assert jobs.heartbeat(conn, job_id, "other", lease_seconds=10, now=16_000) is False


def test_cancel_queued_and_running(conn):
    queued = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.request_cancel(conn, queued)
    assert jobs.get_job(conn, queued).status == "canceled"
    assert jobs.claim(conn, "w1") is None

    running = jobs.enqueue(conn, "synthesize", "book1", 2)
    jobs.claim(conn, "w1")
    jobs.request_cancel(conn, running)
    assert jobs.is_canceled(conn, running) is True


def test_progress_roundtrip(conn):
    job_id = jobs.enqueue(conn, "synthesize", "book1", 1)
    jobs.set_progress(conn, job_id, 3, 10, "第 3 行")
    assert jobs.get_job(conn, job_id).progress == {"done": 3, "total": 10, "message": "第 3 行"}


def test_payload_roundtrip_and_active_lookup(conn):
    job_id = jobs.enqueue(conn, "chapters", "book1", payload={"chapters": [2, 5]})
    job = jobs.get_job(conn, job_id)
    assert job.payload == {"chapters": [2, 5]}
    assert jobs.find_active(conn, "chapters", "book1").id == job_id
    assert jobs.find_active(conn, "chapters", "other-book") is None

    jobs.set_payload(conn, job_id, {"chapters": [2, 5, 7]})
    assert jobs.get_job(conn, job_id).payload == {"chapters": [2, 5, 7]}


def test_payload_survives_progress_updates(conn):
    """参数放 payload、不放 progress：进度写盘不会把章节清单冲掉。"""
    job_id = jobs.enqueue(conn, "chapters", "book1", payload={"chapters": [1]})
    jobs.claim(conn, "w1")
    jobs.set_progress(conn, job_id, 1, 1, "第 1 章")
    job = jobs.get_job(conn, job_id)
    assert job.payload == {"chapters": [1]}
    assert job.progress["message"] == "第 1 章"


def test_progress_carries_inflight_and_keeps_line_target(conn):
    """进度里带"此刻在跑哪几行"（前端画沙漏），且不会把单行任务的目标行冲掉。"""
    job_id = jobs.enqueue_line(conn, "book1", 1, "c0001-s01-l001")
    jobs.set_progress(conn, job_id, 0, 1, "", extra={"inflight": ["c0001-s01-l001"]})
    job = jobs.get_job(conn, job_id)
    assert job.progress["pending_line"] == "c0001-s01-l001"
    assert job.progress["inflight"] == ["c0001-s01-l001"]

    # 下一次不带 inflight 的更新要把旧标记清掉，免得上一轮的沙漏留在页面上
    jobs.set_progress(conn, job_id, 1, 1, "c0001-s01-l001")
    job = jobs.get_job(conn, job_id)
    assert "inflight" not in job.progress
    assert job.progress["pending_line"] == "c0001-s01-l001"
