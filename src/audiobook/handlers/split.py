from .. import jobs, store
from ..text.clean import clean_text
from ..text.split_chapters import split_chapters
from ..worker import register


@register("chapter_split")
def handle_split(ctx, job) -> None:
    book_id = job.book_id
    raw = store.source_path(ctx.settings, book_id).read_text(encoding="utf-8")
    cleaned = clean_text(raw)
    chapters = split_chapters(cleaned.text)
    if not chapters:
        raise RuntimeError("分章结果为空，请检查文本格式")
    store.atomic_replace_json(
        store.chapters_path(ctx.settings, book_id),
        {
            "chapters": [
                {"index": c.index, "title": c.title, "content": c.content, "chars": len(c.content)}
                for c in chapters
            ],
            "clean_stats": cleaned.stats,
            "dropped_lines": cleaned.dropped[:200],
        },
    )
    jobs.enqueue(ctx.conn, "characters", book_id)
    ctx.conn.execute("UPDATE books SET chapter_count=?, status='split' WHERE id=?", (len(chapters), book_id))
    ctx.progress(job, len(chapters), len(chapters), f"{len(chapters)} 章")
