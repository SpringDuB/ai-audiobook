import hashlib
import io
import math
import struct
import time
import wave

from aiab_tts.backends.base import SynthesisRequest, SynthesisResult

EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")
MS_PER_CHAR = 60.0


class StubBackend:
    """测试专用的假后端：不加载任何模型，时长可控、可注入 OOM。产品代码里没有它。"""

    name = "stub-tts"
    version = "stub-1"

    def __init__(self, sample_rate: int = 22050, delay: float = 0.0, oom_on: set[str] | None = None):
        self.sample_rate = sample_rate
        self.delay = delay
        self.oom_on = oom_on or set()
        self._loaded = False
        self.requests: list[SynthesisRequest] = []

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
            "emotionText": True,
            "rate": True,
            "rateRange": [0.5, 2.0],
            "pronunciation": True,
            "pronunciationStyles": ["pinyin"],
            "languages": ["ZH", "EN", "JP", "ES", "AR"],
            "sampleRate": self.sample_rate,
            "maxTextChars": 300,
            "supportsSeed": True,
            "supportsWarmup": True,
            # 按描述生成（Qwen3-TTS 那条路）：逐句给音色描述，不需要参考音频
            "voiceDesign": True,
            "voicePrompt": True,
        }

    def design(self, *, text: str, instruct: str, lang: str = "ZH"):
        """按描述造一段音频（试听接口用），返回 (wav 字节, 采样率, 说的文本)。"""
        result = self.synthesize(
            SynthesisRequest(text=text, voice_prompt=instruct, lang=lang, ref_path=None)
        )
        return result.audio, result.sample_rate, text

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        started = time.monotonic()
        self.requests.append(request)
        if any(token in request.text for token in self.oom_on):
            raise RuntimeError("CUDA out of memory")
        if self.delay:
            time.sleep(self.delay)
        rate = max(0.5, min(2.0, request.rate or 1.0))
        duration = max(0.12, len(request.text) * MS_PER_CHAR / 1000.0) / rate
        seed = request.seed if request.seed is not None else 0
        # 音色描述进摘要：描述不同 → 波形不同（契约测试据此确认描述真的送到了）
        digest = hashlib.sha256(
            f"{request.text}|{seed}|{request.emo_vector}|{request.emotion_text}|{request.lang}"
            f"|{request.voice_prompt}|{request.ref_text}".encode("utf-8")
        ).digest()
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
