import logging

from .. import jobs, store
from ..analysis.casting import build_casting, load_voice_library
from ..analysis.issues import record_issue
from ..worker import register

logger = logging.getLogger(__name__)


@register("casting")
def handle_casting(ctx, job) -> None:
    book_id = job.book_id
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    book_meta = store.read_json(store.book_dir(ctx.settings, book_id) / "book.json", default={}) or {}
    voices = load_voice_library(ctx.settings)
    casting, issues = build_casting(
        characters,
        voices,
        book_id=book_id,
        book_genres=tuple(book_meta.get("genres") or ()),
    )
    for issue in issues:
        record_issue(
            ctx.settings,
            book_id,
            issue["kind"],
            reason=issue["reason"],
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    store.atomic_replace_json(store.casting_path(ctx.settings, book_id), casting)

    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    queued = 0
    for chapter in chapters:
        if store.read_jsonl(store.lines_path(ctx.settings, book_id, chapter["index"])):
            jobs.enqueue(ctx.conn, "synthesize", book_id, chapter["index"])
            queued += 1
    logger.info("选角完成：%d 个角色，入队合成 %d 章", len(casting["roles"]), queued)
    ctx.progress(job, len(casting["roles"]), len(casting["roles"]), f"入队合成 {queued} 章")
