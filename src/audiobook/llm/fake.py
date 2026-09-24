import json
from typing import Callable

from .base import LLMError, LLMRateLimit, LLMReply

RouteValue = dict | Callable[[str], dict]


class FakeLLM:
    """测试用假客户端：按 user prompt 里的标记路由预置 JSON，不访问网络。"""

    def __init__(
        self,
        routes: dict[str, RouteValue] | None = None,
        default: RouteValue | None = None,
        fail_on: set[str] | None = None,
        rate_limit_on: set[str] | None = None,
        model: str = "fake-llm",
    ):
        self.routes = dict(routes or {})
        self.default = default
        self.fail_on = fail_on or set()
        self.rate_limit_on = rate_limit_on or set()
        self.model = model
        self.calls: list[dict] = []

    def _render(self, value: RouteValue, user: str) -> str:
        data = value(user) if callable(value) else value
        return json.dumps(data, ensure_ascii=False)

    def complete(self, system: str, user: str, *, max_output_tokens: int = 4096) -> LLMReply:
        self.calls.append({"system": system, "user": user, "max_output_tokens": max_output_tokens})
        if any(token in user for token in self.fail_on):
            raise LLMError("FakeLLM 注入的失败")
        if any(token in user for token in self.rate_limit_on):
            raise LLMRateLimit("FakeLLM 注入的限流")
        for key, value in self.routes.items():
            if key in user:
                return LLMReply(
                    text=self._render(value, user),
                    model=self.model,
                    input_tokens=10,
                    output_tokens=20,
                    duration_ms=1,
                )
        if self.default is not None:
            return LLMReply(
                text=self._render(self.default, user),
                model=self.model,
                input_tokens=10,
                output_tokens=20,
                duration_ms=1,
            )
        raise LLMError(f"FakeLLM 没有匹配的路由: {user[:60]}")
