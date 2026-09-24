import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  source_path   TEXT NOT NULL,
  chapter_count INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'imported',
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id               INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL,
  book_id          TEXT NOT NULL,
  chapter_index    INTEGER,
  status           TEXT NOT NULL,
  priority         INTEGER NOT NULL DEFAULT 100,
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  not_before       INTEGER,
  lease_expires_at INTEGER,
  worker_id        TEXT,
  progress         TEXT,
  error            TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active
  ON jobs(kind, book_id, IFNULL(chapter_index, -1))
  WHERE status IN ('queued','running');

CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, not_before, priority, id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=1")
