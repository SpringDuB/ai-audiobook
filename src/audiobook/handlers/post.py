import logging

from .. import audio, store
from ..analysis.issues import record_issue
from ..worker import register

logger = logging.getLogger(__name__)


@register("post")
def handle_post(ctx, job) -> None:
    settings, book_id, chapter = ctx.settings, job.book_id, job.chapter_index
    rows = store.read_jsonl(store.lines_path(settings, book_id, chapter))
    if not rows:
        raise RuntimeError(f"第 {chapter} 章没有行数据")
    clips_dir = store.audio_dir(settings, book_id, chapter)
    items: list[tuple] = []
    cues: list[tuple[float, float, str]] = []
    cursor = 0.0
    for row in rows:
        clip = clips_dir / f"{row['id']}.wav"
        if not clip.exists():
            record_issue(
                settings,
                book_id,
                "audio_missing",
                reason="缺少音频片段",
                chapter=chapter,
                line=row["id"],
                fallback="跳过该行，字幕与音频同步偏移",
                detail={"text": row["text"]},
            )
            continue
        duration = audio.wav_duration(clip)
        pause_ms = int(row.get("pause_after_ms") or 0)
        items.append((clip, pause_ms))
        cues.append((cursor, cursor + duration, row["text"]))
        cursor += duration + pause_ms / 1000.0
        ctx.progress(job, len(items), len(rows), row["id"])
    if not items:
        raise RuntimeError("没有任何可拼接的片段")
    tag = store.chapter_tag(chapter)
    wav_path = store.output_dir(settings, book_id) / f"chapter_{tag}.wav"
    srt_path = store.output_dir(settings, book_id) / f"chapter_{tag}.srt"
    total = audio.concat_with_pauses(items, wav_path)
    audio.write_srt(cues, srt_path)
    logger.info("第 %s 章产出完成：%.2fs，%d 条字幕", chapter, total, len(cues))
    ctx.progress(job, len(rows), len(rows), f"完成 {total:.2f}s")
