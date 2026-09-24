import json

from fastapi.testclient import TestClient

from audiobook.api.app import create_app, jobs_snapshot
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


def test_run_endpoint_enqueues_synthesize_for_each_chapter(settings):
    client, conn = make_client(settings)
    book_id = _upload(client, "第一章 重生十年前\n\n正文一。\n\n第二章 死党\n\n正文二。")
    # 先用分章 handler 生成 chapters.json 与 lines
    from audiobook.handlers import split  # noqa: F401
    from audiobook.importer import import_book  # noqa: F401
    from audiobook.worker import WorkerContext, run_once

    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1")
    run_once(ctx)  # chapter_split

    resp = client.post(f"/api/books/{book_id}/run")
    assert resp.status_code == 200
    assert resp.json()["queued"] == 2
