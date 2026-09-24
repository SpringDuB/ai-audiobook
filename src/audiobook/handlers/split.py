from .. import jobs, store
from ..text.clean import clean_text
from ..text.lines_stub import stub_lines
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
    for chapter in chapters:
        rows = stub_lines(chapter.index, chapter.content)
        if not rows:
            continue
        store.write_jsonl_atomic(store.lines_path(ctx.settings, book_id, chapter.index), rows)
        jobs.enqueue(ctx.conn, "synthesize", book_id, chapter.index)
        ctx.progress(job, chapter.index + 1, len(chapters), chapter.title)
    ctx.conn.execute("UPDATE books SET chapter_count=?, status='split' WHERE id=?", (len(chapters), book_id))
