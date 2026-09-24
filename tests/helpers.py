import io
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from audiobook.render.ffmpeg import bundled_ffmpeg

requires_ffmpeg = pytest.mark.skipif(
    bundled_ffmpeg() is None and shutil.which("ffmpeg") is None,
    reason="需要 ffmpeg（项目自带构建缺失且 PATH 里也没有）",
)


def wav_bytes(seconds: float = 0.1, rate: int = 22050) -> bytes:
    frames = int(rate * seconds)
    payload = b"".join(struct.pack("<h", int(8000 * ((index % 100) - 50) / 50)) for index in range(frames))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return stream.getvalue()


def make_voice(settings, voice_id: str = "v_test", content: bytes = b"RIFFfake") -> Path:
    path = settings.voices_dir / voice_id / "ref.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def write_tone(path: Path, *, seconds: float, rate: int, freq: float, amp: float = 0.2) -> Path:
    """写一段正弦波 wav，用来验证重采样/响度是否真的生效。"""
    frames = int(seconds * rate)
    payload = b"".join(
        struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * freq * index / rate)))
        for index in range(frames)
    )
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stream.getvalue())
    return path


def estimate_freq(path: Path, start: float, end: float) -> float:
    """用过零率估算区间主频（±0.5%），够用来判定"有没有被当成别的采样率播"。"""
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    segment = samples[int(start * rate): int(end * rate)]
    crossings = sum(1 for index in range(1, len(segment)) if (segment[index - 1] < 0) != (segment[index] < 0))
    return crossings / 2.0 / (len(segment) / rate)
