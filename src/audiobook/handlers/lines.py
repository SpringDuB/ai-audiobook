from .. import jobs, store
from ..analysis.attribution import names_from_payload
from ..analysis.chapter import analyze_chapter, dump_chapter_analysis, load_chapter_analysis, materialize
from ..analysis.characters import ensure_characters
from ..analysis.issues import record_issue
from ..analysis.pronounce import load_pronounce_table
from ..analysis.readiness import casting_ready
from ..text.dialogue import split_units
from ..worker import register
from .characters import require_llm


@register("lines")
def handle_lines(ctx, job) -> None:
    book_id, chapter_index = job.book_id, job.chapter_index
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    chapter = next((item for item in chapters if item["index"] == chapter_index), None)
    if chapter is None:
        raise RuntimeError(f"chapters.json 里没有第 {chapter_index} 章")
    units = split_units(chapter["content"])
    if not units:
        raise RuntimeError(f"第 {chapter_index} 章没有可分析的句子")

    analysis_path = store.chapter_analysis_path(ctx.settings, book_id, chapter_index)
    payload = store.read_json(analysis_path)
    if not payload:
        # 本章还没分析过（例如单独点「分析本章」）：在这里补跑一次整章分析
        runner = require_llm(ctx)
        existing = store.read_json(store.characters_path(ctx.settings, book_id), default={}) or {}
        result = analyze_chapter(
            runner,
            settings=ctx.settings,
            book_id=book_id,
            chapter_index=chapter_index,
            title=chapter.get("title") or f"第{chapter_index}章",
            content=chapter["content"],
            units=units,
            known_names=names_from_payload(existing),
            on_window=lambda done, total, span: ctx.progress(job, done, total, span),
        )
        store.atomic_replace_json(analysis_path, dump_chapter_analysis(result.analysis))
        for issue in result.issues:
            record_issue(
                ctx.settings,
                book_id,
                issue["kind"],
                reason=issue["reason"],
                chapter=chapter_index,
                fallback=issue.get("fallback"),
                detail=issue.get("detail"),
            )
        payload = dump_chapter_analysis(result.analysis)

    analysis = load_chapter_analysis(payload)
    characters = store.read_json(store.characters_path(ctx.settings, book_id), default={}) or {}
    characters, added = ensure_characters(characters, analysis.characters)
    if added:
        store.atomic_replace_json(store.characters_path(ctx.settings, book_id), characters)

    lines, issues = materialize(
        chapter_index=chapter_index,
        units=units,
        annotations=analysis.lines,
        characters_payload=characters,
        pronounce_table=load_pronounce_table(ctx.settings),
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
