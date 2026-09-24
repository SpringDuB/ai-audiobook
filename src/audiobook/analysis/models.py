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
