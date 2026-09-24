from pydantic import BaseModel, ConfigDict, Field, field_validator


class SynthPayload(BaseModel):
    """POST /v1/synthesize 的请求体（字段名与冻结契约一致）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str
    refId: str  # noqa: N815 - 线上字段名固定为驼峰
    lang: str = "ZH"
    emoVector: list[float] | None = None  # noqa: N815
    rate: float = 1.0
    pronunciation: dict[str, str] = Field(default_factory=dict)
    seed: int | None = None
    format: str = "wav"

    @field_validator("text")
    @classmethod
    def _text_required(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("text 不能为空")
        return value

    @field_validator("rate")
    @classmethod
    def _rate_range(cls, value: float) -> float:
        if not 0.5 <= float(value) <= 2.0:
            raise ValueError("rate 必须在 0.5–2.0 之间")
        return float(value)

    @field_validator("emoVector")
    @classmethod
    def _vector_length(cls, value):
        if value is not None and len(value) != 8:
            raise ValueError("emoVector 必须是 8 维")
        return value

    @field_validator("format")
    @classmethod
    def _format_supported(cls, value: str) -> str:
        if value != "wav":
            raise ValueError("目前只支持 wav")
        return value
