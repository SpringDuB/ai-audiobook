from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    ref_path: Path
    ref_text: str = ""
    lang: str = "ZH"
    emo_vector: tuple[float, ...] | None = None
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
