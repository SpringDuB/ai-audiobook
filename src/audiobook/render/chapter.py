import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import store
from . import srt as srt_mod
from .container import write_media
from .ffmpeg import probe_wav
from .loudness import LoudnessResult, normalize_to_file
from .mix import Clip, clip_from_wav, concat_clips, ensure_uniform
from .naming import chapter_label
from .pauses import build_pause_plan

RENDER_VERSION = 1

# 章节重渲染后必须作废的整本级产物（下一次 book_export 会重建它们）
BOOK_LEVEL_ARTIFACTS = ("playlist.m3u", "book_章节.txt", "merge-report.txt")


@dataclass(frozen=True)
class ChapterRenderResult:
    wav: Path
    srt: Path
    container: Path | None
    duration: float
    cues: int
    sample_rate: int
    clips: int = 0
    loudness: LoudnessResult | None = None
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cached: bool = False


def build_clips(settings, book_id: str, chapter_index: int) -> tuple[list[Clip], list[str]]:
    rows = store.read_jsonl(store.lines_path(settings, book_id, chapter_index))
    if not rows:
        raise RuntimeError(f"第 {chapter_index} 章没有行数据，请先跑 lines 任务")
    pauses = build_pause_plan(rows, settings)
    clips_dir = store.audio_dir(settings, book_id, chapter_index)
    clips: list[Clip] = []
    skipped: list[str] = []
    for row, pause_ms in zip(rows, pauses):
        path = clips_dir / f"{row['id']}.wav"
        if not path.exists():
            skipped.append(row["id"])
            continue
        clips.append(clip_from_wav(row["id"], path, pause_ms=pause_ms, text=row.get("text") or ""))
    return clips, skipped


def _stamp(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return ["missing", str(path)]
    return [stat.st_mtime_ns, stat.st_size]


def render_key(settings, clips: list[Clip]) -> str:
    payload = {
        "version": RENDER_VERSION,
        "pause": [
            settings.pause_scale,
            settings.pause_min_ms,
            settings.pause_max_ms,
            settings.pause_scene_extra_ms,
            settings.pause_tail_ms,
        ],
        "loudness": [
            settings.loudness_mode,
            settings.loudness_target_lufs,
            settings.loudness_true_peak,
            settings.loudness_rms_target_db,
        ],
        "sample_rate": settings.export_target_sample_rate,
        "container": settings.export_container if settings.export_mkv else "",
        "clips": [
            [
                clip.line_id,
                clip.pause_ms,
                round(clip.duration, 4),
                clip.sample_rate,
                clip.channels,
                clip.bits,
                _stamp(clip.path),
            ]
            for clip in clips
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def invalidate_book_products(settings, book_id: str) -> list[Path]:
    """章节变了，整本成品就不再可信：删掉它们，让 plan/export 重新生成。"""
    removed: list[Path] = []
    candidates = [
        store.book_wav_path(settings, book_id),
        store.book_srt_path(settings, book_id),
        store.book_media_path(settings, book_id, "mkv"),
        store.book_media_path(settings, book_id, "mp4"),
        *(store.output_dir(settings, book_id) / name for name in BOOK_LEVEL_ARTIFACTS),
    ]
    for path in candidates:
        if Path(path).exists():
            Path(path).unlink()
            removed.append(Path(path))
    return removed


def _plan_cues(clips: list[Clip]) -> tuple[list[srt_mod.Cue], float]:
    cues: list[srt_mod.Cue] = []
    cursor = 0.0
    for clip in clips:
        cues.append(srt_mod.Cue(start=cursor, end=cursor + clip.duration, text=clip.text))
        cursor += clip.duration + clip.pause_ms / 1000.0
    return cues, cursor


def render_chapter(
    settings, book_id: str, chapter_index: int, *, force: bool = False, on_progress=None
) -> ChapterRenderResult:
    clips, skipped = build_clips(settings, book_id, chapter_index)
    if not clips:
        raise RuntimeError(f"第 {chapter_index} 章没有任何可拼接的音频片段")
    key = render_key(settings, clips)
    wav_path = store.chapter_wav_path(settings, book_id, chapter_index)
    srt_path = store.chapter_srt_path(settings, book_id, chapter_index)
    meta_path = store.chapter_render_meta_path(settings, book_id, chapter_index)
    container_path = (
        store.chapter_media_path(settings, book_id, chapter_index, settings.export_container)
        if settings.export_mkv
        else None
    )
    previous = store.read_json(meta_path, default={}) or {}
    if (
        not force
        and previous.get("render_key") == key
        and wav_path.exists()
        and srt_path.exists()
        and (container_path is None or container_path.exists())
    ):
        return ChapterRenderResult(
            wav=wav_path,
            srt=srt_path,
            container=container_path,
            duration=float(previous.get("duration") or 0.0),
            cues=int(previous.get("cues") or 0),
            sample_rate=int(previous.get("sample_rate") or 0),
            clips=int(previous.get("clips") or len(clips)),
            cached=True,
            skipped=tuple(skipped),
        )

    def report(done: int, total: int, message: str) -> None:
        if on_progress is not None:
            on_progress(done, total, message)

    work_dir = store.render_work_dir(settings, book_id, chapter_index)
    report(1, 4, "统一采样率")
    uniform, target_rate = ensure_uniform(settings, clips, work_dir)
    if settings.pause_tail_ms and uniform:
        # 章节尾部静音 = 最后一句自身停顿 + pause_tail_ms
        uniform = [*uniform[:-1], replace(uniform[-1], pause_ms=uniform[-1].pause_ms + settings.pause_tail_ms)]
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = wav_path.with_name(f".{wav_path.name}.tmp")
    report(2, 4, "拼接片段")
    concat_clips(uniform, tmp_path)
    report(3, 4, "响度归一")
    loudness = normalize_to_file(settings, tmp_path, wav_path, sample_rate=target_rate)
    tmp_path.unlink(missing_ok=True)
    info = probe_wav(wav_path)
    cues, planned = _plan_cues(uniform)
    warnings: list[str] = []
    if abs(planned - info.duration) > 0.25:
        warnings.append(f"字幕时间轴 {planned:.2f}s 与音频 {info.duration:.2f}s 不一致")
    srt_mod.write_srt(cues, srt_path)
    if container_path is not None:
        report(4, 4, "封装容器")
        write_media(
            settings,
            audio=wav_path,
            subtitles=srt_path,
            dst=container_path,
            container=settings.export_container,
            title=chapter_label(settings, book_id, chapter_index),
        )
    store.atomic_replace_json(
        meta_path,
        {
            "render_key": key,
            "sample_rate": target_rate,
            "duration": round(info.duration, 3),
            "cues": len(cues),
            "clips": len(clips),
            "skipped": list(skipped),
            "warnings": warnings,
            "loudness": loudness.as_dict(),
            "container": container_path.name if container_path else None,
            "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        },
    )
    shutil.rmtree(work_dir, ignore_errors=True)
    invalidate_book_products(settings, book_id)
    return ChapterRenderResult(
        wav=wav_path,
        srt=srt_path,
        container=container_path,
        duration=info.duration,
        cues=len(cues),
        sample_rate=target_rate,
        clips=len(clips),
        loudness=loudness,
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )
