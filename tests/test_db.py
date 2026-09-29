from audiobook.db import connect, init_db


def test_init_db_creates_tables_and_uses_wal(tmp_path):
    conn = connect(tmp_path / "service.db")
    init_db(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"books", "jobs"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_init_db_adds_payload_column_to_old_jobs_table(tmp_path):
    """老库（没有 payload 列）升级时自动补列，任务参数不用重建库。"""
    conn = connect(tmp_path / "service.db")
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, book_id TEXT NOT NULL,"
        " chapter_index INTEGER, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,"
        " attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,"
        " not_before INTEGER, lease_expires_at INTEGER, worker_id TEXT, progress TEXT, error TEXT,"
        " cancel_requested INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,"
        " updated_at INTEGER NOT NULL)"
    )
    init_db(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "payload" in columns


def test_init_db_is_idempotent(tmp_path):
    conn = connect(tmp_path / "service.db")
    init_db(conn)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
