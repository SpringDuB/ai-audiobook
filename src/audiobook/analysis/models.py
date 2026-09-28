"""分析链的数据模型：提取（整章一次）/ 角色整合 / 音色推荐。

分析结果全部由大模型直出，这里的模型只负责严格校验与归一化：
提取阶段出「每句话 + 说话人 + 对白情绪」；整合阶段出「主名 + 别名」；
推荐阶段出「1–3 个音色 id + 置信度 + 理由」。
"""

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

# 情绪枚举：中文名，落盘与 UI 用它；合成时再翻成引擎的 8 维向量维度名
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
    """提取阶段的一行：原文句子 + 说话人 + （仅人物话术的）情绪。

    extra="ignore"：一整章会输出上百条记录，多一个字段不该让整段白跑；
    缺字段/写错类型的记录会在落盘阶段按可见的降级规则处理。
    """

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    role: str = "未知"
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
    """整章提取的返回值：严格的 JSON 数组。"""


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
