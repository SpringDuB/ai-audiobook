from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class EngineCapabilities:
    name: str
    version: str
    emotions: bool
    emotion_dims: tuple[str, ...]
    rate: bool
    pronunciation: bool
    sample_rate: int
    max_text_chars: int = 300


@dataclass(frozen=True)
class SynthParams:
    emo_vector: tuple[float, ...] | None = None
    rate: float = 1.0
    lang: str | None = "ZH"
    pronunciation: dict[str, str] | None = None


@dataclass(frozen=True)
class AudioResult:
    path: Path
    duration: float
    sample_rate: int


class EngineAdapter(Protocol):
    def capabilities(self) -> EngineCapabilities: ...

    def synthesize(self, text: str, voice_id: str, params: SynthParams | None, out_path: Path) -> AudioResult: ...


def summarize_params(params: SynthParams | None, caps: EngineCapabilities) -> dict:
    """只保留引擎实际支持的参数 —— 缓存键必须基于这份结果。"""
    if params is None:
        return {}
    summary: dict = {}
    if caps.emotions and params.emo_vector is not None:
        summary["emo_vector"] = [round(v, 4) for v in params.emo_vector]
    if caps.rate:
        summary["rate"] = round(params.rate, 4)
    if params.lang:
        summary["lang"] = params.lang
    if caps.pronunciation and params.pronunciation:
        summary["pronunciation"] = dict(sorted(params.pronunciation.items()))
    return summary
