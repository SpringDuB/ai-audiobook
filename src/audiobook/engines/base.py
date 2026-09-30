from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# 分析侧产出中文情绪名（喜悦/愤怒/…），IndexTTS 的 8 维向量用英文名（happy/angry/…）：
# 少了这次翻译，emo_vector 会一直是 None —— 情绪根本没送到模型。
EMOTION_DIM_ALIASES = {
    "喜悦": "happy",
    "愤怒": "angry",
    "悲伤": "sad",
    "恐惧": "afraid",
    "厌恶": "disgusted",
    "忧郁": "melancholic",
    "惊讶": "surprised",
    "平静": "calm",
}


def dim_name_for(emotion_name: str | None, dims: tuple[str, ...]) -> str | None:
    """中文情绪名 → 引擎维度名；引擎不认识就返回 None（不硬塞）。"""
    if not emotion_name:
        return None
    if emotion_name in dims:
        return emotion_name
    mapped = EMOTION_DIM_ALIASES.get(emotion_name)
    return mapped if mapped in dims else None


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
    # 服务端是否支持"用一句话描述情绪"（需要它加载了 QwenEmotion）
    emotion_text: bool = False
    # 服务端是否支持批量合成（同一个音色多条文本一次解码）
    batch: bool = False
    max_batch_items: int = 1


@dataclass(frozen=True)
class SynthParams:
    emo_vector: tuple[float, ...] | None = None
    emotion_text: str | None = None
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
    if caps.emotion_text and params.emotion_text:
        summary["emotion_text"] = params.emotion_text
    if caps.rate:
        summary["rate"] = round(params.rate, 4)
    if params.lang:
        summary["lang"] = params.lang
    if caps.pronunciation and params.pronunciation:
        summary["pronunciation"] = dict(sorted(params.pronunciation.items()))
    return summary
