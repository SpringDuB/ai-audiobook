import time

import httpx

from .base import LLMError, LLMRateLimit, LLMReply, LLMTimeout


class OpenAICompatClient:
    """OpenAI 兼容 /chat/completions 客户端（deepseek-flash、vLLM、one-api 网关通用）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        temperature: float = 0.6,
        json_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.json_mode = json_mode
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def complete(self, system: str, user: str, *, max_output_tokens: int = 4096) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_output_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            resp = self._client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"LLM 请求超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code == 429:
            raise LLMRateLimit(f"LLM 限流: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - 任何结构异常都按协议错误处理
            raise LLMError(f"LLM 响应结构异常: {exc}") from exc
        usage = data.get("usage") or {}
        return LLMReply(
            text=content or "",
            model=data.get("model") or self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            duration_ms=duration_ms,
        )


def build_client(settings) -> OpenAICompatClient:
    if not settings.llm_base_url:
        raise LLMError("未配置 LLM 端点：请设置 AB_LLM_BASE_URL，例如 http://127.0.0.1:8009/v1")
    return OpenAICompatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        json_mode=settings.llm_json_mode,
    )
