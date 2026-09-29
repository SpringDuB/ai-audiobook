from .. import store


def casting_ready(settings, conn, book_id: str, exclude_job_id: int | None = None) -> bool:
    """所有章都有非空 lines，且没有**其他**活跃分析任务 → 可以选角。"""
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        return False
    for chapter in chapters:
        if not store.read_jsonl(store.lines_path(settings, book_id, chapter["index"])):
            return False
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE book_id=? AND kind IN ('characters','chapters','lines')"
        " AND status IN ('queued','running') AND id IS NOT ?",
        (book_id, exclude_job_id),
    ).fetchone()
    return row["n"] == 0
