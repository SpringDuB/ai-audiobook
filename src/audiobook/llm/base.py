from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    """LLM 调用失败（网络、协议、响应结构）。"""


class LLMRateLimit(LLMError):
    """服务端限流（429 或等价信号）。"""


class LLMTimeout(LLMError):
    """请求超时。"""


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 推理型模型单独记账的思考 tokens（同一个 max_tokens 预算里的）
    reasoning_tokens: int | None = None
    duration_ms: int = 0


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *, max_output_tokens: int = 4096) -> LLMReply: ...
