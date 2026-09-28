from pydantic import BaseModel, ConfigDict, Field, field_validator

GENDERS = ("男", "女", "中性", "未知")
AGE_GROUPS = ("儿童", "少年", "青年", "中年", "老年", "未知")
EMOTIONS = ("喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静")
DELIVERIES = ("normal", "shout", "whisper", "sneer")
NARRATOR_NAMES = ("旁白", "叙述", "旁白叙述")


def clamp01(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


class CharacterCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    aliases: list[str] = Field(default_factory=list)
    gender: str = "未知"
    age_group: str = "未知"
    personality: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    base_emotion: str = "平静"
    base_intensity: float = 0.4

    @field_validator("aliases", "personality", mode="before")
    @classmethod
    def _list_or_empty(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("speaking_style", mode="before")
    @classmethod
    def _str_or_empty(cls, value):
        return "" if value is None else value

    @field_validator("base_intensity", mode="before")
    @classmethod
    def _clamp(cls, value):
        return clamp01(value)


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    closeness: float = 0.5
    hierarchy: float = 0.5
    hostility: float = 0.0
    intimacy: float = 0.0
    note: str = ""

    @field_validator("closeness", "hierarchy", "hostility", "intimacy", mode="before")
    @classmethod
    def _clamp(cls, value):
        return clamp01(value)


class PassAOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters: list[CharacterCard]
    relationships: list[Relationship]


class LineAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    speaker: str
    addressee: str | None = None
    emotion: str = "继承"
    intensity: float | None = None
    # 副情绪：表面情绪底下藏着的那层（如"笑着威胁"＝喜悦 + 愤怒），让合成更像人而不是念稿
    secondary: str | None = None
    secondary_weight: float | None = None
    delivery: str = "normal"

    @field_validator("emotion", "delivery", "secondary", mode="before")
    @classmethod
    def _str_or_default(cls, value, info):
        if value is None:
            if info.field_name == "secondary":
                return None
            return "继承" if info.field_name == "emotion" else "normal"
        return value

    @field_validator("intensity", "secondary_weight", mode="before")
    @classmethod
    def _clamp(cls, value):
        return None if value is None else clamp01(value)


class PassCOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[LineAnnotation]
