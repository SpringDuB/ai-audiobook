from audiobook.db import connect, init_db


def test_init_db_creates_tables_and_uses_wal(tmp_path):
    conn = connect(tmp_path / "service.db")
    init_db(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"books", "jobs"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_init_db_is_idempotent(tmp_path):
    conn = connect(tmp_path / "service.db")
    init_db(conn)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
