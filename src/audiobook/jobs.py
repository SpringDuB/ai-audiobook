import json
import sqlite3
import time
from dataclasses import dataclass


def now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    book_id: str
    chapter_index: int | None
    status: str
    attempts: int
    max_attempts: int
    progress: dict | None
    error: str | None = None
    created_at: int = 0
    updated_at: int = 0


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        book_id=row["book_id"],
        chapter_index=row["chapter_index"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        progress=json.loads(row["progress"]) if row["progress"] else None,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(conn: sqlite3.Connection, book_id: str | None = None) -> list[Job]:
    if book_id is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs WHERE book_id=? ORDER BY id", (book_id,)).fetchall()
    return [_row_to_job(r) for r in rows]


def enqueue(conn, kind, book_id, chapter_index=None, priority=100, max_attempts=3, now=None) -> int:
    """幂等入队：同一 (kind, book, chapter) 在未完成状态下复用同一条记录。"""
    ts = now_ms(now)
    key = -1 if chapter_index is None else chapter_index
    for _ in range(3):
        row = conn.execute(
            "SELECT id FROM jobs WHERE kind=? AND book_id=? AND IFNULL(chapter_index,-1)=?"
            " AND status IN ('queued','running')",
            (kind, book_id, key),
        ).fetchone()
        if row:
            return row["id"]
        try:
            cur = conn.execute(
                "INSERT INTO jobs(kind, book_id, chapter_index, status, priority, max_attempts,"
                " created_at, updated_at) VALUES(?,?,?,'queued',?,?,?,?)",
                (kind, book_id, chapter_index, priority, max_attempts, ts, ts),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(f"enqueue 冲突未解决: {kind}/{book_id}/{chapter_index}")


def claim(conn, worker_id, lease_seconds=30, now=None) -> Job | None:
    ts = now_ms(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0"
            " AND (not_before IS NULL OR not_before<=?) ORDER BY priority, id LIMIT 1",
            (ts,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status='running', worker_id=?, lease_expires_at=?,"
            " attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
            (worker_id, ts + lease_seconds * 1000, ts, row["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_job(conn, row["id"])


def heartbeat(conn, job_id, worker_id, lease_seconds=30, now=None) -> bool:
    ts = now_ms(now)
    cur = conn.execute(
        "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? AND worker_id=? AND status='running'",
        (ts + lease_seconds * 1000, ts, job_id, worker_id),
    )
    return cur.rowcount == 1


def complete(conn, job_id, worker_id, now=None) -> None:
    ts = now_ms(now)
    conn.execute(
        "UPDATE jobs SET status='done', lease_expires_at=NULL, updated_at=? WHERE id=?",
        (ts, job_id),
    )


def fail(conn, job_id, worker_id, error, retry_delay_ms=None, now=None) -> str:
    ts = now_ms(now)
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if retry_delay_ms is None:
        retry_delay_ms = min(60_000, 1000 * 2 ** max(0, job.attempts - 1))
    if job.attempts >= job.max_attempts:
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, lease_expires_at=NULL, updated_at=? WHERE id=?",
            (str(error), ts, job_id),
        )
        return "failed"
    conn.execute(
        "UPDATE jobs SET status='queued', error=?, not_before=?, worker_id=NULL,"
        " lease_expires_at=NULL, updated_at=? WHERE id=?",
        (str(error), ts + retry_delay_ms, ts, job_id),
    )
    return "queued"


def reap_expired(conn, now=None) -> int:
    ts = now_ms(now)
    rows = conn.execute(
        "SELECT id, attempts, max_attempts FROM jobs"
        " WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
        (ts,),
    ).fetchall()
    for row in rows:
        if row["attempts"] >= row["max_attempts"]:
            conn.execute(
                "UPDATE jobs SET status='failed', error='租约过期且已达最大重试次数', updated_at=? WHERE id=?",
                (ts, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='queued', worker_id=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (ts, row["id"]),
            )
    return len(rows)


def set_progress(conn, job_id, done, total, message="", now=None) -> None:
    ts = now_ms(now)
    payload = json.dumps({"done": done, "total": total, "message": message}, ensure_ascii=False)
    conn.execute("UPDATE jobs SET progress=?, updated_at=? WHERE id=?", (payload, ts, job_id))


def request_cancel(conn, job_id, now=None) -> None:
    ts = now_ms(now)
    job = get_job(conn, job_id)
    if job is None or job.status in ("done", "failed", "canceled"):
        return
    if job.status == "queued":
        conn.execute("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (ts, job_id))
    else:
        conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (ts, job_id))


def retry(conn, job_id: int, now=None) -> bool:
    """把失败/取消的任务重新排队（清零 attempts，清掉错误与退避）。"""
    ts = now_ms(now)
    cur = conn.execute(
        "UPDATE jobs SET status='queued', attempts=0, error=NULL, not_before=NULL,"
        " worker_id=NULL, lease_expires_at=NULL, cancel_requested=0, updated_at=?"
        " WHERE id=? AND status IN ('failed','canceled')",
        (ts, job_id),
    )
    return cur.rowcount == 1


def enqueue_line(conn, book_id: str, chapter_index: int, line_id: str, now=None) -> int:
    """单行重合成任务：具体行 id 记在 progress.pending_line 上。"""
    job_id = enqueue(conn, "synthesize_line", book_id, chapter_index, now=now)
    conn.execute(
        "UPDATE jobs SET progress=? WHERE id=?",
        (json.dumps({"pending_line": line_id}, ensure_ascii=False), job_id),
    )
    return job_id


def is_canceled(conn, job_id) -> bool:
    row = conn.execute("SELECT status, cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return True
    return row["status"] == "canceled" or row["cancel_requested"] == 1
