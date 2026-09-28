from .. import jobs, store
from ..analysis.issues import record_issue
from ..analysis.lines import process_chapter
from ..analysis.pronounce import load_pronounce_table
from ..analysis.readiness import casting_ready
from ..text.split_chapters import split_sentences
from ..worker import register
from .characters import require_llm


@register("lines")
def handle_lines(ctx, job) -> None:
    runner = require_llm(ctx)
    book_id, chapter_index = job.book_id, job.chapter_index
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    chapter = next((item for item in chapters if item["index"] == chapter_index), None)
    if chapter is None:
        raise RuntimeError(f"chapters.json 里没有第 {chapter_index} 章")
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    sentences = split_sentences(chapter["content"])
    if not sentences:
        raise RuntimeError(f"第 {chapter_index} 章没有可分析的句子")

    lines, issues = process_chapter(
        runner,
        settings=ctx.settings,
        book_id=book_id,
        chapter_index=chapter_index,
        title=chapter.get("title") or f"第{chapter_index}章",
        sentences=sentences,
        characters_payload=characters,
        pronounce_table=load_pronounce_table(ctx.settings),
        on_window=lambda done, total, span: ctx.progress(job, done, total, span),
    )
    if not lines:
        raise RuntimeError(f"第 {chapter_index} 章没有产出任何行")
    store.write_jsonl_atomic(store.lines_path(ctx.settings, book_id, chapter_index), lines)
    for issue in issues:
        record_issue(
            ctx.settings,
            book_id,
            issue["kind"],
            reason=issue["reason"],
            chapter=chapter_index,
            scene=issue.get("scene"),
            line=issue.get("line"),
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    if casting_ready(ctx.settings, ctx.conn, book_id, exclude_job_id=job.id):
        jobs.enqueue(ctx.conn, "casting", book_id)
    ctx.progress(job, len(lines), len(lines), f"{len(lines)} 句")
