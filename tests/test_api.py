import json

from fastapi.testclient import TestClient

from audiobook.api.app import JobStream, create_app, jobs_snapshot
from audiobook.db import connect, init_db


def make_client(settings):
    conn = connect(settings.db_path)
    init_db(conn)
    return TestClient(create_app(settings, conn)), conn


def _upload(client, text: str):
    resp = client.post(
        "/api/books",
        files={"file": ("solar.txt", text.encode("utf-8"))},
        data={"title": "地球最后一个修仙者"},
    )
    assert resp.status_code == 200
    return resp.json()["book_id"]


def test_upload_creates_book_and_split_job(settings):
    client, _ = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    assert client.get("/api/books").json()["books"][0]["id"] == book_id
    job_kinds = [j["kind"] for j in client.get("/api/jobs", params={"book_id": book_id}).json()["jobs"]]
    assert job_kinds == ["chapter_split"]


def test_cancel_job(settings):
    client, _ = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    job_id = client.get("/api/jobs", params={"book_id": book_id}).json()["jobs"][0]["id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    status = client.get("/api/jobs", params={"book_id": book_id}).json()["jobs"][0]["status"]
    assert status == "canceled"


def test_events_route_registered_and_snapshot_contains_jobs(settings):
    """SSE 是无限流，TestClient 会一直等响应体，所以这里只验证路由注册与快照内容；真实流式行为在手工验收里用 curl 验证。"""
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    paths = {route.path for route in client.app.routes}
    assert "/api/events" in paths
    payload = json.dumps(jobs_snapshot(conn, book_id), ensure_ascii=False)
    assert "chapter_split" in payload


def test_jobs_snapshot_has_no_timestamp_and_keeps_window(settings):
    """快照不能带每秒都变的 ts（否则"变了才发"永远成立），并且只留窗口。"""
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    for index in range(45):
        conn.execute(
            "INSERT INTO jobs(kind, book_id, chapter_index, status, attempts, max_attempts,"
            " created_at, updated_at) VALUES('post', ?, ?, 'done', 1, 3, ?, ?)",
            (book_id, index + 100, index, index),
        )
    conn.commit()
    snapshot = jobs_snapshot(conn, book_id)
    assert "ts" not in snapshot
    assert len(snapshot["jobs"]) <= 31  # 1 条活跃 + 最近 30 条
    assert snapshot["jobs"][-1]["chapter_index"] == 144  # 最近的那条


def test_job_stream_only_emits_when_something_changes(settings):
    """首帧 snapshot，之后没变化就不发字节，有变化只发 patch。"""
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    job_id = client.get("/api/jobs", params={"book_id": book_id}).json()["jobs"][0]["id"]
    stream = JobStream(conn, book_id)

    first = stream.poll()
    assert first is not None and '"type": "snapshot"' in first and "chapter_split" in first
    assert stream.poll() is None  # 空闲：一帧都不发

    conn.execute("UPDATE jobs SET status='running', updated_at=updated_at+1 WHERE id=?", (job_id,))
    conn.commit()
    patch = stream.poll()
    assert patch is not None
    assert '"type": "patch"' in patch and f'"id": {job_id}' in patch
    assert patch.count('"kind"') == 1  # patch 里只带变化的那一条，不是整份窗口
    assert stream.poll() is None


def test_job_stream_legacy_frame_keeps_jobs_field(settings):
    """老前端（缓存的旧 JS）只认 jobs 字段：不带 v=2 时仍然给窗口全量。"""
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。")
    stream = JobStream(conn, book_id, delta=False)
    frame = stream.poll()
    assert '"jobs"' in frame and '"type"' not in frame


def test_tts_status_reports_configured_engine(settings):
    # 起点显式给成"没配端点"：.env 里可能已经写了端点，别让用例依赖本机配置
    settings = settings.model_copy(update={"engine": "http", "tts_endpoints": []})
    client, _ = make_client(settings)
    payload = client.get("/api/tts/status").json()
    # 没有端点时不是"假装健康"，而是给一条能照做的错误
    assert payload["engine"] == "http"
    assert payload["concurrency"] == 0
    assert payload["endpoints"] == []
    assert "一键启动" in payload["error"]


def test_chapter_analyze_only_queues_that_chapter(settings):
    """「分析本章」：只重跑这一章，没有角色表也能单独跑（新称呼由 lines 增量补进角色表）。"""
    from audiobook import store

    client, conn = make_client(settings)
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        ("b1", "测试书", "source/original.txt", 2, "split", 1790000000000),
    )
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"),
        {
            "chapters": [
                {"index": 1, "title": "第一章", "content": "第一句。", "chars": 4},
                {"index": 2, "title": "第二章", "content": "第二句。", "chars": 4},
            ]
        },
    )

    payload = client.post("/api/books/b1/chapters/2/analyze").json()
    assert payload["plan"] == [["lines", 2]]
    kinds = [
        (job["kind"], job["chapter_index"])
        for job in client.get("/api/jobs", params={"book_id": "b1"}).json()["jobs"]
    ]
    assert sorted(kinds, key=str) == [("lines", 2)]
    assert not store.characters_path(settings, "b1").exists()

    # 换一章同样只推那一章
    conn.execute("UPDATE jobs SET status='done'")
    payload = client.post("/api/books/b1/chapters/1/analyze").json()
    assert payload["plan"] == [["lines", 1]]
    assert client.post("/api/books/b1/chapters/9/analyze").status_code == 404


def test_run_endpoint_enqueues_next_pipeline_step(settings):
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。\n\n第二章 死党\n\n正文二。")
    # 先用分章 handler 生成 chapters.json
    from audiobook.handlers import split  # noqa: F401
    from audiobook.importer import import_book  # noqa: F401
    from audiobook.worker import WorkerContext, run_once

    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1")
    run_once(ctx)  # chapter_split

    resp = client.post(f"/api/books/{book_id}/run")
    assert resp.status_code == 200
    # 有分章、还没有角色档案 → 下一步是 characters
    assert resp.json()["queued"] == 1
    assert resp.json()["plan"] == [["characters", None]]
