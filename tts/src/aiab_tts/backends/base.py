from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    # 参考音频：给了就走音色克隆（Qwen3-TTS Base / IndexTTS）；
    # 没给但带 voice_prompt 时走"按描述生成"（Qwen3-TTS VoiceDesign）
    ref_path: Path | None = None
    ref_text: str = ""
    lang: str = "ZH"
    # 这一句的音色描述（角色基础描述 + 本句语气描述拼好的那一句）
    voice_prompt: str = ""
    emo_vector: tuple[float, ...] | None = None
    # 自然语言情绪描述（"压着火气、语速比平时快"）：需要服务端加载了 QwenEmotion
    emotion_text: str = ""
    rate: float = 1.0
    pronunciation: dict[str, str] = field(default_factory=dict)
    seed: int | None = None


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    duration_sec: float
    sample_rate: int
    engine: str
    engine_version: str
    elapsed_ms: int


class TtsBackend(Protocol):
    name: str
    version: str

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def is_loaded(self) -> bool: ...

    def capabilities(self) -> dict: ...

    def recommended_concurrency(self) -> int: ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult: ...
