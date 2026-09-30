import logging

from .. import jobs, store
from ..analysis.issues import record_issue
from .synthesize import synth_line
from ..worker import register

logger = logging.getLogger(__name__)


@register("synthesize_line")
def handle_synthesize_line(ctx, job) -> None:
    ctx.raise_if_cancelled(job)
    line_id = (job.progress or {}).get("pending_line")
    if not line_id:
        raise RuntimeError("synthesize_line 缺少 pending_line")
    rows = store.read_jsonl(store.lines_path(ctx.settings, job.book_id, job.chapter_index))
    if not rows:
        raise RuntimeError(f"第 {job.chapter_index} 章没有行数据")
    row = next((item for item in rows if item["id"] == line_id), None)
    if row is None:
        raise RuntimeError(f"{line_id} 不在第 {job.chapter_index} 章")
    ctx.progress(job, 0, 1, "", extra={"inflight": [line_id]})
    try:
        result = synth_line(ctx, job, row)
    except Exception as exc:  # noqa: BLE001 - 单行失败必须可见
        record_issue(
            ctx.settings,
            job.book_id,
            "tts_line_failed",
            reason=f"{type(exc).__name__}: {exc}",
            chapter=job.chapter_index,
            line=line_id,
            fallback="该行保持原有音频",
        )
        raise
    logger.info("单行重合成完成：%s %.2fs（cached=%s）", line_id, result["duration"], result["cached"])
    ctx.progress(job, 1, 1, f"{line_id} {result['duration']:.2f}s")
    jobs.enqueue(ctx.conn, "post", job.book_id, job.chapter_index)
