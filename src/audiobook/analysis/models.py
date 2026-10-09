"""分析链的数据模型：提取（整章一次）/ 角色整合 / 音色描述 / 音色推荐。

分析结果全部由大模型直出，这里的模型只负责严格校验与归一化：
提取阶段出「每句话 + 说话人」；整合阶段出「主名 + 别名」；
音色描述阶段出「音色描述 + 试音台词」（喂 Qwen3-TTS VoiceDesign）；
推荐阶段（库存音色，旁白用）出「1–3 个音色 id + 置信度 + 理由」。
"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

# 情绪枚举：老数据的兼容位（IndexTTS 那套 8 维向量用），Qwen3-TTS 不再用
EMOTIONS = ("喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静")
DELIVERIES = ("normal", "shout", "whisper", "sneer")
NARRATOR_NAMES = ("旁白", "叙述", "旁白叙述")
UNKNOWN_ROLES = ("未知", "未知角色", "?", "？")


def clamp01(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def is_narrator(name: str | None) -> bool:
    return (name or "").strip() in NARRATOR_NAMES


def is_unknown(name: str | None) -> bool:
    return (name or "").strip() in UNKNOWN_ROLES


class SpokenLine(BaseModel):
    """提取阶段的一行：原文句子 + 说话人 + 这一句的表演描述。

    extra="ignore"：一整章会输出上百条记录，多一个字段不该让整段白跑；
    缺字段/写错类型的记录会在落盘阶段按可见的降级规则处理。
    voice 是"这一句怎么说"（语气/语速/情绪），和角色基础音色描述拼起来喂给 TTS；
    emotion 等字段是历史数据的兼容位：新提取不再输出，也不参与合成。
    """

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    role: str = "未知"
    voice: str | None = None
    emotion: str | None = None
    intensity: float | None = None
    secondary: str | None = None
    secondary_weight: float | None = None

    @field_validator("text", mode="before")
    @classmethod
    def _strip_text(cls, value):
        return str(value or "").strip()

    @field_validator("role", mode="before")
    @classmethod
    def _strip_role(cls, value):
        return str(value or "").strip() or "未知"

    @field_validator("voice", mode="before")
    @classmethod
    def _clean_voice(cls, value):
        text = str(value or "").strip()
        return text[:80] or None

    @field_validator("emotion", "secondary", mode="before")
    @classmethod
    def _known_emotion(cls, value):
        text = str(value or "").strip()
        return text if text in EMOTIONS else None

    @field_validator("intensity", "secondary_weight", mode="before")
    @classmethod
    def _clamp(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            return round(clamp01(value), 3)
        except (TypeError, ValueError):
            return None


class ExtractionOutput(RootModel[list[SpokenLine]]):
    """整章提取的返回值：JSON 数组（也兼容被包成对象的数组）。

    提示词要求顶层是数组，但 OpenAI 兼容接口的 JSON mode（response_format=
    {"type":"json_object"}）强制顶层必须是对象 —— 模型只能把数组塞进某个字段，
    实测出现过 {"type":"json_object","lines":[...]} 这种回显式包装。这里统一拆包，
    别让一层包装白白废掉整段提取（30k tokens 的重跑代价）。
    """

    # 常见的包装字段名，优先按这些取；没有就退化成"取第一个数组值"
    WRAPPER_KEYS: ClassVar[tuple[str, ...]] = (
        "lines", "items", "data", "result", "list", "array", "segments", "output",
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_array(cls, value):
        if not isinstance(value, dict):
            return value
        for key in cls.WRAPPER_KEYS:
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
        for inner in value.values():
            if isinstance(inner, list):
                return inner
        return value


class CharacterMerge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return str(value or "").strip()

    @field_validator("aliases", mode="before")
    @classmethod
    def _list_or_empty(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class MergeOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    characters: list[CharacterMerge] = Field(default_factory=list)


class VoiceRecommendation(BaseModel):
    """音色推荐的一条：voiceId 用驼峰（与大模型输出、前端字段一致）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    voice_id: str = Field(alias="voiceId")
    confidence: float = 0.5
    reason: str = ""

    @field_validator("voice_id", mode="before")
    @classmethod
    def _strip_id(cls, value):
        return str(value or "").strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value):
        return 0.5 if value is None else round(clamp01(value), 3)

    @field_validator("reason", mode="before")
    @classmethod
    def _short_reason(cls, value):
        return str(value or "").strip()[:60]


class RecommendOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recommendations: list[VoiceRecommendation] = Field(default_factory=list)


class VoiceDesignOutput(BaseModel):
    """音色描述阶段的返回值：给 VoiceDesign 的一句话描述 + 一句试音台词。"""

    model_config = ConfigDict(extra="ignore")

    description: str = ""
    sample: str = ""

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value):
        return str(value or "").strip()[:200]

    @field_validator("sample", mode="before")
    @classmethod
    def _clean_sample(cls, value):
        return str(value or "").strip()[:80]


class VoiceArchetype(BaseModel):
    """选角表的一行：角色名 + 音色原型（音区/质地/年龄感）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    archetype: str = ""

    @field_validator("name", "archetype", mode="before")
    @classmethod
    def _strip(cls, value):
        return str(value or "").strip()

    @field_validator("archetype", mode="after")
    @classmethod
    def _short(cls, value):
        return value[:80]


class CastSheetOutput(BaseModel):
    """选角表的返回值：整本书的角色 → 音色原型。"""

    model_config = ConfigDict(extra="ignore")

    characters: list[VoiceArchetype] = Field(default_factory=list)
