import hashlib
import io
import math
import struct
import time
import wave

from .base import SynthesisRequest, SynthesisResult

EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")
MS_PER_CHAR = 60.0


class FakeBackend:
    """不加载任何模型的参照后端：时长可控、可注入 OOM，用于本地端到端验证。"""

    name = "fake-tts"
    version = "fake-1"

    def __init__(self, sample_rate: int = 22050, delay: float = 0.0, oom_on: set[str] | None = None):
        self.sample_rate = sample_rate
        self.delay = delay
        self.oom_on = oom_on or set()
        self._loaded = False

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def recommended_concurrency(self) -> int:
        return 4

    def capabilities(self) -> dict:
        return {
            "engine": self.name,
            "engineVersion": self.version,
            "emotions": True,
            "emotionDims": list(EMOTION_DIMS),
            "rate": True,
            "rateRange": [0.5, 2.0],
            "pronunciation": True,
            "pronunciationStyles": ["pinyin"],
            "languages": ["ZH", "EN", "JP", "ES", "AR"],
            "sampleRate": self.sample_rate,
            "maxTextChars": 300,
            "supportsSeed": True,
            "supportsWarmup": True,
        }

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        started = time.monotonic()
        if any(token in request.text for token in self.oom_on):
            raise RuntimeError("CUDA out of memory")
        if self.delay:
            time.sleep(self.delay)
        rate = max(0.5, min(2.0, request.rate or 1.0))
        duration = max(0.12, len(request.text) * MS_PER_CHAR / 1000.0) / rate
        seed = request.seed if request.seed is not None else 0
        digest = hashlib.sha256(f"{request.text}|{seed}|{request.emo_vector}|{request.lang}".encode("utf-8")).digest()
        base_freq = 180.0 + digest[0]
        frames = int(self.sample_rate * duration)
        period = max(1, int(round(self.sample_rate / base_freq)))
        single = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * base_freq * index / self.sample_rate)))
            for index in range(period)
        )
        payload = (single * (frames // period + 1))[: frames * 2]
        stream = io.BytesIO()
        with wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(payload)
        return SynthesisResult(
            audio=stream.getvalue(),
            duration_sec=frames / self.sample_rate,
            sample_rate=self.sample_rate,
            engine=self.name,
            engine_version=self.version,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
