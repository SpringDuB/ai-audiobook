from dataclasses import dataclass, replace
from pathlib import Path

from ..audio import concat_with_pauses
from .ffmpeg import probe_wav, run_ffmpeg

TARGET_CHANNELS = 1
TARGET_BITS = 16


@dataclass(frozen=True)
class Clip:
    line_id: str
    path: Path
    pause_ms: int = 0
    text: str = ""
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = TARGET_CHANNELS
    bits: int = TARGET_BITS


def clip_from_wav(line_id: str, path: Path, *, pause_ms: int = 0, text: str = "") -> Clip:
    info = probe_wav(Path(path))
    return Clip(
        line_id=line_id,
        path=Path(path),
        pause_ms=pause_ms,
        text=text,
        duration=info.duration,
        sample_rate=info.sample_rate,
        channels=info.channels,
        bits=info.bits,
    )


def plan_target_rate(settings, clips: list[Clip]) -> int:
    if settings.export_target_sample_rate:
        return int(settings.export_target_sample_rate)
    if not clips:
        raise ValueError("没有片段，无法确定目标采样率")
    return max(clip.sample_rate for clip in clips)


def _needs_conversion(clip: Clip, target_rate: int) -> bool:
    return not (clip.sample_rate == target_rate and clip.channels == TARGET_CHANNELS and clip.bits == TARGET_BITS)


def ensure_uniform(settings, clips: list[Clip], work_dir: Path) -> tuple[list[Clip], int]:
    """混采样率必须显式重采样：直接按帧拼接会静默变速变调。"""
    target_rate = plan_target_rate(settings, clips)
    if not any(_needs_conversion(clip, target_rate) for clip in clips):
        return list(clips), target_rate
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    converted: list[Clip] = []
    for clip in clips:
        if not _needs_conversion(clip, target_rate):
            converted.append(clip)
            continue
        dst = work_dir / f"{clip.line_id}.wav"
        run_ffmpeg(
            settings,
            [
                "-i",
                str(clip.path),
                "-ar",
                str(target_rate),
                "-ac",
                str(TARGET_CHANNELS),
                "-c:a",
                "pcm_s16le",
                str(dst),
            ],
        )
        info = probe_wav(dst)
        converted.append(
            replace(
                clip,
                path=dst,
                sample_rate=info.sample_rate,
                channels=info.channels,
                bits=info.bits,
                duration=info.duration,
            )
        )
    return converted, target_rate


def concat_clips(clips: list[Clip], out_path: Path) -> float:
    return concat_with_pauses([(clip.path, clip.pause_ms) for clip in clips], Path(out_path))
