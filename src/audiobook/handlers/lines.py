"""分析链第二步：把提取结果物化成行记录（role_id / 语速 / 语言 / 注音）。

提取结果已经在（或由本 handler 现跑）analysis/extract/chapter_XXXX.json；
这里不再做任何切句判断，只做名字→role_id 的映射与可见的降级处理。
标注与旧结果不一致时，顺带作废本章成品音频，让「生成有声书」按新标注重建。
"""

import logging

from .. import jobs, store
from ..analysis.extract import dump_extraction, extract_chapter, load_extraction
from ..analysis.issues import record_issue
from ..analysis.materialize import materialize
from ..analysis.merge import extend_characters
from ..analysis.models import is_narrator
from ..analysis.pronounce import load_pronounce_table
from ..analysis.readiness import casting_ready
from ..analysis.roles import names_from_payload, resolve_speaker
from ..render.chapter import invalidate_chapter_products
from ..worker import register
from .characters import require_llm

logger = logging.getLogger(__name__)

# 只有这些字段影响音频；标注重跑但内容没变时不该作废已合成的成品
SYNTHESIS_KEYS = (
    "id", "kind", "speaker", "speaker_name", "text",
    "emotion", "delivery", "rate", "lang", "pronounce",
)


def _synthesis_signature(rows: list[dict]) -> list[dict]:
    return [{key: row.get(key) for key in SYNTHESIS_KEYS} for row in rows]


def _spoken_of(ctx, job, chapter) -> list:
    path = store.extract_path(ctx.settings, job.book_id, job.chapter_index)
    payload = store.read_json(path)
    if payload:
        return load_extraction(payload)
    # 本章还没提取过（例如单独点「分析本章」）：这里补跑一次
    runner = require_llm(ctx)
    existing = store.read_json(store.characters_path(ctx.settings, job.book_id), default={}) or {}
    result = extract_chapter(
        runner,
        book_id=job.book_id,
        chapter_index=job.chapter_index,
        title=chapter.get("title") or f"第{job.chapter_index}章",
        content=chapter["content"],
        window_chars=ctx.settings.llm_line_window_chars,
        known_names=names_from_payload(existing),
        on_window=lambda done, total, span: ctx.progress(job, done, total, span),
    )
    store.atomic_replace_json(path, dump_extraction(result))
    for issue in result.issues:
        record_issue(
            ctx.settings,
            job.book_id,
            issue["kind"],
            reason=issue["reason"],
            chapter=job.chapter_index,
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    return result.lines


def _characters_for(ctx, job, spoken) -> dict:
    characters = store.read_json(store.characters_path(ctx.settings, job.book_id), default={}) or {}
    unknown = [
        item.role
        for item in spoken
        if not is_narrator(item.role) and resolve_speaker(characters, item.role) is None
    ]
    if not unknown:
        return characters
    characters, issues = extend_characters(
        require_llm(ctx), book_id=job.book_id, payload=characters, spoken=spoken
    )
    for issue in issues:
        record_issue(
            ctx.settings,
            job.book_id,
            issue["kind"],
            reason=issue["reason"],
            chapter=job.chapter_index,
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    store.atomic_replace_json(store.characters_path(ctx.settings, job.book_id), characters)
    return characters


@register("lines")
def handle_lines(ctx, job) -> None:
    book_id, chapter_index = job.book_id, job.chapter_index
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    chapter = next((item for item in chapters if item["index"] == chapter_index), None)
    if chapter is None:
        raise RuntimeError(f"chapters.json 里没有第 {chapter_index} 章")

    spoken = _spoken_of(ctx, job, chapter)
    characters = _characters_for(ctx, job, spoken)
    lines, issues = materialize(
        chapter_index=chapter_index,
        spoken=spoken,
        characters_payload=characters,
        pronounce_table=load_pronounce_table(ctx.settings),
    )
    if not lines:
        raise RuntimeError(f"第 {chapter_index} 章没有产出任何行")
    lines_path = store.lines_path(ctx.settings, book_id, chapter_index)
    previous = store.read_jsonl(lines_path)
    store.write_jsonl_atomic(lines_path, lines)
    if _synthesis_signature(previous) != _synthesis_signature(lines):
        # 标注变了：本章与整本成品都不再可信，删掉让「生成有声书」重建
        removed = invalidate_chapter_products(ctx.settings, book_id, chapter_index)
        logger.info("第 %s 章标注变化：作废 %d 个成品文件", chapter_index, len(removed))
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
