from .. import store
from ..text.clean import clean_text
from ..text.split_chapters import split_chapters
from ..worker import register


def split_book(settings, conn, book_id: str) -> list[dict]:
    """清洗 + 分章并落盘；handler 与 migrate 都走这一条路径。"""
    raw = store.source_path(settings, book_id).read_text(encoding="utf-8")
    cleaned = clean_text(raw)
    chapters = split_chapters(cleaned.text)
    if not chapters:
        raise RuntimeError("分章结果为空，请检查文本格式")
    payload = [
        {"index": c.index, "title": c.title, "content": c.content, "chars": len(c.content)} for c in chapters
    ]
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": payload, "clean_stats": cleaned.stats, "dropped_lines": cleaned.dropped[:200]},
    )
    meta_path = store.book_dir(settings, book_id) / "book.json"
    meta = store.read_json(meta_path, default={}) or {}
    store.atomic_replace_json(meta_path, {**meta, "status": "split", "chapter_count": len(chapters)})
    conn.execute("UPDATE books SET chapter_count=?, status='split' WHERE id=?", (len(chapters), book_id))
    return payload


@register("chapter_split")
def handle_split(ctx, job) -> None:
    book_id = job.book_id
    chapters = split_book(ctx.settings, ctx.conn, book_id)
    # 分章只是本地文本处理（不花 LLM）。分析链由用户点「一键分析 / 分析角色文本」触发，
    # 否则导入一本书就会立刻把大模型请求打出去。
    ctx.progress(job, len(chapters), len(chapters), f"{len(chapters)} 章")
