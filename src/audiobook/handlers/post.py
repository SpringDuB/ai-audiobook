import logging

from ..analysis.issues import record_issue
from ..listen import ensure_mobile_audio
from ..render.chapter import render_chapter
from ..worker import register

logger = logging.getLogger(__name__)


@register("post")
def handle_post(ctx, job) -> None:
    ctx.raise_if_cancelled(job)
    settings, book_id, chapter = ctx.settings, job.book_id, job.chapter_index

    def progress(done: int, total: int, message: str) -> None:
        ctx.progress(job, done, total, message)

    # 幂等：输入与设置没变时 render_chapter 直接复用已有产物（不重编码）
    result = render_chapter(settings, book_id, chapter, force=False, on_progress=progress)
    if not result.cached:
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
    # 顺手转好手机离线听的 m4a：手机下载整本时就不用一章一章现场等 ffmpeg
    # （14 分钟一章约 9s；m4a 比 wav 新就直接复用，重渲染后自动重转）
    if bool(getattr(settings, "mobile_audio_prewarm", True)):
        try:
            ensure_mobile_audio(settings, book_id, chapter)
        except Exception:  # noqa: BLE001 - 预热失败不该影响成品，下次下载会再试
            logger.warning("预转手机音频失败（不影响成品，下载时会重试）", exc_info=True)
    ctx.progress(job, 5, 5, f"完成 {result.duration:.2f}s")
