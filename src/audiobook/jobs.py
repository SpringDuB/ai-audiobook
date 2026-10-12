import json
import sqlite3
import time
from collections.abc import Collection
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
    # 任务参数（如批量分析的章节清单）；progress 是执行中的进度，会被覆盖，别把参数塞那里
    payload: dict | None = None
    # 用户点了取消但 worker 还没停下来：前端据此显示"取消中"
    cancel_requested: bool = False


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
        payload=json.loads(row["payload"]) if row["payload"] else None,
        cancel_requested=bool(row["cancel_requested"]),
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    book_id: str | None = None,
    *,
    statuses: list[str] | None = None,
    limit: int | None = None,
) -> list[Job]:
    """任务列表。默认全量（老契约不变）；传 statuses/limit 时在 SQL 里就过滤掉。

    任务只会越攒越多（实测 2400+ 条 = 630KB JSON），把不需要的行也读出来再序列化
    是纯浪费：任务中心只展示运行中/排队中/最近的失败与完成。
    """
    clauses: list[str] = []
    params: list[object] = []
    if book_id is not None:
        clauses.append("book_id=?")
        params.append(book_id)
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    sql = "SELECT * FROM jobs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if limit is not None and int(limit) > 0:
        # "最近 N 条"：倒序取完再翻回正序，前端拿到的顺序和全量时一致
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, int(limit)]).fetchall()
        rows.reverse()
    else:
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [_row_to_job(r) for r in rows]


def enqueue(
    conn, kind, book_id, chapter_index=None, priority=100, max_attempts=3, now=None, payload=None
) -> int:
    """幂等入队：同一 (kind, book, chapter) 在未完成状态下复用同一条记录。"""
    ts = now_ms(now)
    key = -1 if chapter_index is None else chapter_index
    payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
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
                " payload, created_at, updated_at) VALUES(?,?,?,'queued',?,?,?,?,?)",
                (kind, book_id, chapter_index, priority, max_attempts, payload_json, ts, ts),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(f"enqueue 冲突未解决: {kind}/{book_id}/{chapter_index}")


def find_active(conn, kind, book_id, chapter_index=None) -> Job | None:
    """找同键的排队/运行中任务（enqueue 幂等复用的就是这一条）。"""
    key = -1 if chapter_index is None else chapter_index
    row = conn.execute(
        "SELECT * FROM jobs WHERE kind=? AND book_id=? AND IFNULL(chapter_index,-1)=?"
        " AND status IN ('queued','running') ORDER BY id LIMIT 1",
        (kind, book_id, key),
    ).fetchone()
    return _row_to_job(row) if row else None


def set_payload(conn, job_id: int, payload: dict | None, now=None) -> None:
    ts = now_ms(now)
    raw = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?", (raw, ts, job_id))


def _placeholders(values: Collection[str]) -> str:
    return ",".join("?" for _ in values)


def claim(
    conn,
    worker_id,
    lease_seconds=30,
    now=None,
    *,
    kinds: Collection[str] | None = None,
    exclude_kinds: Collection[str] | None = None,
    max_running: tuple[Collection[str], int] | None = None,
    exclusive_per_book: Collection[str] | None = None,
    exclusive_per_chapter: Collection[str] | None = None,
) -> Job | None:
    """领取一个排队任务。

    并发策略不写死在队列里，由调用方（worker）用过滤条件表达，见 worker.run_forever：

    - ``kinds`` / ``exclude_kinds``：只领 / 不领某些 kind —— 整章合成（吃 GPU）
      与其余任务走两条通道，互不堵；
    - ``max_running=(kinds, limit)``：这组 kind 全局最多同时跑 limit 条。数的是库里
      ``status='running'`` 的行，所以多开 worker 进程也拦得住；
    - ``exclusive_per_book``：这组 kind 在同一本书里同时只跑一条 —— 分析链的任务会
      读改写同一份 characters.json / casting.json，并发读改写会互相覆盖；
    - ``exclusive_per_chapter``：这组 kind 在同一本书同一章里同时只跑一条 —— 合成与
      渲染碰的是同一章的片段 / 成品，重叠会读到写了一半的结果。

    默认（不带任何条件）只按 ``priority, id`` 顺序领一个可跑的任务。
    """
    ts = now_ms(now)
    sql = (
        "SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0"
        " AND (not_before IS NULL OR not_before<=?)"
    )
    params: list[object] = [ts]
    if kinds:
        group = tuple(sorted(set(kinds)))
        sql += f" AND kind IN ({_placeholders(group)})"
        params.extend(group)
    if exclude_kinds:
        group = tuple(sorted(set(exclude_kinds)))
        sql += f" AND kind NOT IN ({_placeholders(group)})"
        params.extend(group)
    if max_running:
        group, limit = tuple(sorted(set(max_running[0]))), int(max_running[1])
        if group:
            marks = _placeholders(group)
            sql += (
                f" AND (kind NOT IN ({marks}) OR (SELECT COUNT(*) FROM jobs r"
                f" WHERE r.status='running' AND r.kind IN ({marks})) < ?)"
            )
            params.extend(group)
            params.extend(group)
            params.append(limit)
    if exclusive_per_book:
        group = tuple(sorted(set(exclusive_per_book)))
        if group:
            marks = _placeholders(group)
            sql += (
                f" AND (kind NOT IN ({marks}) OR NOT EXISTS (SELECT 1 FROM jobs r"
                f" WHERE r.status='running' AND r.book_id=jobs.book_id AND r.kind IN ({marks})))"
            )
            params.extend(group)
            params.extend(group)
    if exclusive_per_chapter:
        group = tuple(sorted(set(exclusive_per_chapter)))
        if group:
            marks = _placeholders(group)
            sql += (
                f" AND (kind NOT IN ({marks}) OR NOT EXISTS (SELECT 1 FROM jobs r"
                f" WHERE r.status='running' AND r.book_id=jobs.book_id"
                f" AND IFNULL(r.chapter_index,-1)=IFNULL(jobs.chapter_index,-1)"
                f" AND r.kind IN ({marks})))"
            )
            params.extend(group)
            params.extend(group)
    sql += " ORDER BY priority, id LIMIT 1"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(sql, params).fetchone()
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
        "SELECT id, attempts, max_attempts, cancel_requested FROM jobs"
        " WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
        (ts,),
    ).fetchall()
    for row in rows:
        if row["cancel_requested"]:
            # 已经点过取消：worker 没了也算取消成功，别再排队重跑
            conn.execute(
                "UPDATE jobs SET status='canceled', lease_expires_at=NULL, worker_id=NULL,"
                " updated_at=? WHERE id=?",
                (ts, row["id"]),
            )
        elif row["attempts"] >= row["max_attempts"]:
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


def set_progress(conn, job_id, done, total, message="", extra=None, now=None) -> None:
    """更新任务进度。

    ``extra`` 用来带执行细节（比如整章合成时"此刻在跑哪几行"），前端靠它把
    正在生成的段落画上沙漏动画。老键值会保留（``pending_line`` 这类任务参数
    就存在 progress 里，覆盖掉的话单行重合成失败重试会找不到行），但
    ``inflight`` 每次调用都重算，免得上一轮的标记残留。
    """
    ts = now_ms(now)
    row = conn.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()
    payload: dict = {}
    raw = row["progress"] if row is not None else None
    if raw:
        try:
            payload = json.loads(raw) or {}
        except (TypeError, ValueError):
            payload = {}
    payload.update({"done": done, "total": total, "message": message})
    payload.pop("inflight", None)
    if extra:
        payload.update(extra)
    conn.execute(
        "UPDATE jobs SET progress=?, updated_at=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), ts, job_id),
    )


def request_cancel(conn, job_id, now=None) -> None:
    ts = now_ms(now)
    job = get_job(conn, job_id)
    if job is None or job.status in ("done", "failed", "canceled"):
        return
    if job.status == "queued":
        conn.execute("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (ts, job_id))
    else:
        conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (ts, job_id))
        # 没有 worker 在跑（租约早过期）时没人会来读这个标记 → 直接落成取消
        row = conn.execute(
            "SELECT lease_expires_at FROM jobs WHERE id=? AND status='running'", (job_id,)
        ).fetchone()
        lease = row["lease_expires_at"] if row is not None else None
        if lease is not None and lease < ts:
            mark_canceled(conn, job_id, now=ts)


def mark_canceled(conn, job_id, now=None) -> bool:
    """把执行中/排队中的任务落成 canceled（worker 收到取消、或租约过期时调用）。"""
    ts = now_ms(now)
    cur = conn.execute(
        "UPDATE jobs SET status='canceled', worker_id=NULL, lease_expires_at=NULL, updated_at=?"
        " WHERE id=? AND status IN ('queued','running')",
        (ts, job_id),
    )
    return cur.rowcount == 1


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


def enqueue_line(conn, book_id: str, chapter_index: int, line_id: str, now=None, max_attempts: int = 3) -> int:
    """单行重合成任务：具体行 id 记在 progress.pending_line 上。"""
    job_id = enqueue(conn, "synthesize_line", book_id, chapter_index, now=now, max_attempts=max_attempts)
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
