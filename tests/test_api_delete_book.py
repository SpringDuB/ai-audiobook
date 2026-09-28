import pytest

from audiobook import jobs, store

from test_api_read import _client, _conn, _seed_book


def test_delete_book_removes_row_dir_and_jobs(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    conn = _conn(settings)
    jobs.enqueue(conn, "chapter_split", book_id)

    response = client.delete(f"/api/books/{book_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert (body["title"], body["removed_jobs"], body["dir_removed"]) == ("测试书", 1, True)
    assert client.get("/api/books").json()["books"] == []
    assert not store.book_dir(settings, book_id).exists()
    left = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE book_id=?", (book_id,)).fetchone()["n"]
    assert left == 0


def test_delete_book_waits_for_running_job(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    conn = _conn(settings)
    jobs.enqueue(conn, "chapter_split", book_id)
    assert jobs.claim(conn, "w_test", lease_seconds=60) is not None

    response = client.delete(f"/api/books/{book_id}")

    assert response.status_code == 409
    assert "正在运行" in response.json()["detail"]
    assert store.book_dir(settings, book_id).exists()
    assert [book["id"] for book in client.get("/api/books").json()["books"]] == [book_id]


def test_delete_book_ignores_stale_running_lease(settings, narrator_lines):
    """worker 崩了留下的 running 残骸（租约已过期）不该把删除永久卡住。"""
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    conn = _conn(settings)
    jobs.enqueue(conn, "chapter_split", book_id)
    assert jobs.claim(conn, "w_dead", lease_seconds=-5) is not None

    assert client.delete(f"/api/books/{book_id}").status_code == 200
    assert not store.book_dir(settings, book_id).exists()


def test_delete_book_404_for_unknown_and_400_for_bad_id(settings):
    client = _client(settings)
    assert client.delete("/api/books/nope").status_code == 404
    assert client.delete("/api/books/bad.id").status_code == 400


def test_store_delete_book_rejects_path_traversal(settings, conn):
    with pytest.raises(ValueError):
        store.delete_book(settings, conn, "../outside")
