import wave
from pathlib import Path


class FormatMismatch(RuntimeError):
    pass


def _params(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path)) as fh:
        return fh.getframerate(), fh.getnchannels(), fh.getsampwidth()


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as fh:
        return fh.getnframes() / float(fh.getframerate())


def concat_with_pauses(items: list[tuple[Path, int]], out_path: Path) -> float:
    """把同格式片段按顺序拼接，并在每段之后插入 pause_after_ms 毫秒静音。"""
    if not items:
        raise ValueError("没有可拼接的片段")
    fmt = _params(items[0][0])
    for path, _ in items:
        if _params(path) != fmt:
            raise FormatMismatch(f"采样率/声道/位深不一致: {path}")
    rate, channels, width = fmt
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    total_frames = 0
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        for path, pause_ms in items:
            with wave.open(str(path)) as src:
                frames = src.readframes(src.getnframes())
            out.writeframes(frames)
            total_frames += len(frames) // (channels * width)
            if pause_ms and pause_ms > 0:
                silence_frames = int(rate * pause_ms / 1000.0)
                out.writeframes(b"\x00" * silence_frames * channels * width)
                total_frames += silence_frames
    tmp.replace(out_path)
    return total_frames / float(rate)


def format_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(cues: list[tuple[float, float, str]], path: Path) -> None:
    from .store import atomic_write_text

    blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):
        blocks.append(f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n")
    atomic_write_text(Path(path), "\n".join(blocks))
