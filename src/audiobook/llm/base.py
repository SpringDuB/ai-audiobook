from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    """LLM 调用失败（网络、协议、响应结构）。"""

    def __init__(self, message: str, *, duration_ms: int | None = None):
        super().__init__(message)
        # 失败也要能记时长：网关掐连接时，日志里全是 0ms 会看不出"卡了 60 秒才断"
        self.duration_ms = duration_ms


class LLMRateLimit(LLMError):
    """服务端限流（429 或等价信号）。"""


class LLMTimeout(LLMError):
    """请求超时。"""


class LLMDisconnected(LLMError):
    """连接被服务端/中间层掐断（Server disconnected、SSL EOF、连接重置）。

    长请求在非流式下常见：中间层等不到任何字节就按空闲超时切连接。属于"降并发重试就好"的信号。
    """


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
