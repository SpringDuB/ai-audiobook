from pydantic import BaseModel, ConfigDict, Field, field_validator


class SynthItem(BaseModel):
    """一条待合成文本的参数（批量接口里的一项；单条接口的字段是它的超集）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str
    emoVector: list[float] | None = None  # noqa: N815
    # 两条情绪通道二选一：emoText（文本描述，需服务端 use_qwen_emo）优先于 emoVector
    emoText: str | None = None  # noqa: N815
    rate: float = 1.0
    pronunciation: dict[str, str] = Field(default_factory=dict)
    seed: int | None = None

    @field_validator("text")
    @classmethod
    def _text_required(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("text 不能为空")
        return value

    @field_validator("emoText")
    @classmethod
    def _emo_text_len(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) > 120:
            raise ValueError("emoText 太长了（≤120 字），请写一句简短的情绪描述")
        return text

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

    @field_validator("pronunciation", mode="before")
    @classmethod
    def _pronunciation_none(cls, value):
        return {} if value is None else value


class SynthPayload(SynthItem):
    """POST /v1/synthesize 的请求体（字段名与冻结契约一致）。"""

    refId: str  # noqa: N815 - 线上字段名固定为驼峰
    lang: str = "ZH"
    format: str = "wav"

    @field_validator("format")
    @classmethod
    def _format_supported(cls, value: str) -> str:
        if value != "wav":
            raise ValueError("目前只支持 wav")
        return value

class BatchSynthPayload(BaseModel):
    """POST /v1/synthesize_batch 的请求体：同一个音色的多条文本一起解码。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    refId: str  # noqa: N815
    lang: str = "ZH"
    items: list[SynthItem]

    @field_validator("items")
    @classmethod
    def _items_present(cls, value: list[SynthItem]) -> list[SynthItem]:
        if not value:
            raise ValueError("items 不能为空")
        if len(value) > 16:
            raise ValueError("一次最多 16 条（再多显存吃不住）")
        return value
