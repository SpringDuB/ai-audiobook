import logging

from ..analysis.issues import record_issue
from ..render.chapter import render_chapter
from ..worker import register

logger = logging.getLogger(__name__)


@register("post")
def handle_post(ctx, job) -> None:
    settings, book_id, chapter = ctx.settings, job.book_id, job.chapter_index

    def progress(done: int, total: int, message: str) -> None:
        ctx.progress(job, done, total, message)

    result = render_chapter(settings, book_id, chapter, force=True, on_progress=progress)
    for line_id in result.skipped:
        record_issue(
            settings,
            book_id,
            "audio_missing",
            reason="缺少音频片段",
            chapter=chapter,
            line=line_id,
            fallback="跳过该行，字幕与音频同步偏移",
        )
    for warning in result.warnings:
        record_issue(
            settings,
            book_id,
            "render_duration_mismatch",
            reason=warning,
            chapter=chapter,
            fallback="以实际音频时长为准，请复核该章字幕",
        )
    logger.info(
        "第 %s 章产出完成：%.2fs，%d 条字幕，%d 个片段（跳过 %d）",
        chapter,
        result.duration,
        result.cues,
        result.clips,
        len(result.skipped),
    )
    ctx.progress(job, 5, 5, f"完成 {result.duration:.2f}s")
