import math
import struct
import time
import wave
from pathlib import Path

from .base import AudioResult, EngineCapabilities, SynthParams

EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")


class FakeEngine:
    """测试用引擎：时长与文本长度成正比，可注入失败与延迟，输出真实可播放的 WAV。"""

    def __init__(
        self,
        sample_rate: int = 22050,
        ms_per_char: float = 45.0,
        fail_on: set[str] | None = None,
        delay: float = 0.0,
    ):
        self.sample_rate = sample_rate
        self.ms_per_char = ms_per_char
        self.fail_on = fail_on or set()
        self.delay = delay

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="fake",
            version="fake-1",
            emotions=True,
            emotion_dims=EMOTION_DIMS,
            rate=True,
            pronunciation=True,
            sample_rate=self.sample_rate,
            max_text_chars=300,
        )

    def synthesize(self, text: str, voice_id: str, params: SynthParams | None, out_path: Path) -> AudioResult:
        if any(token in text for token in self.fail_on):
            raise RuntimeError(f"FakeEngine 注入的失败: {text[:20]}")
        if self.delay:
            time.sleep(self.delay)
        rate = (params.rate if params else 1.0) or 1.0
        duration = max(0.05, len(text) * self.ms_per_char / 1000.0) / rate
        frames = int(self.sample_rate * duration)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        base = 220.0 + (abs(hash(voice_id)) % 200)
        # 生成一个完整周期后复制，避免逐样本 Python 循环拖慢长文本
        period = max(1, int(round(self.sample_rate / base)))
        single = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * base * i / self.sample_rate))) for i in range(period)
        )
        payload = (single * (frames // period + 1))[: frames * 2]
        with wave.open(str(out_path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(self.sample_rate)
            fh.writeframes(payload)
        return AudioResult(path=out_path, duration=frames / self.sample_rate, sample_rate=self.sample_rate)
