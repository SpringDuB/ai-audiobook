import logging

from ..render.book import export_book
from ..worker import register

logger = logging.getLogger(__name__)


@register("book_export")
def handle_book_export(ctx, job) -> None:
    def progress(done: int, total: int, message: str) -> None:
        ctx.progress(job, done, total, message)

    report = export_book(ctx.settings, job.book_id, mode="all", on_progress=progress)
    for warning in report.warnings:
        logger.warning("%s", warning)
    logger.info(
        "整本导出完成：%d 章 / %.2fs / %d 条字幕 / %d 个产物",
        len(report.chapters),
        report.total_seconds,
        report.cues,
        len(report.outputs),
    )
    ctx.progress(job, 1, 1, f"完成 {report.total_seconds:.2f}s")
