import json
import time

import httpx

from .base import LLMDisconnected, LLMError, LLMRateLimit, LLMReply, LLMTimeout


class _UnsupportedStreaming(RuntimeError):
    """网关不认 stream / stream_options 参数，需要降级重试。"""

    def __init__(self, body: str):
        super().__init__(body)
        self.body = body


# 流式响应里查询取消状态的间隔（秒）：每个 SSE 块都查一次太浪费，太慢又打断不及时
CANCEL_CHECK_INTERVAL = 0.3


def _check_cancel(cancel_check) -> None:
    if cancel_check is not None:
        cancel_check()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _body_text(resp: httpx.Response) -> str:
    try:
        return resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 错误响应读不出来也不能盖掉原始错误
        return ""


class OpenAICompatClient:
    """OpenAI 兼容 /chat/completions 客户端（deepseek-flash、vLLM、one-api 网关通用）。

    默认走 SSE 流式：非流式长请求在几十秒里一个字节都收不到，中间层会按空闲超时掐连接
    （Server disconnected / SSL EOF），而模型那边其实已经生成完并计费。流式持续回包，
    连接不会被判空闲；网关不认 stream 参数时自动降级成一次性响应。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        temperature: float = 0.6,
        json_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
        stream: bool = True,
    ):
        self.model = model
        self.temperature = temperature
        self.json_mode = json_mode
        self.stream = bool(stream)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 4096,
        cancel_check=None,
        json_mode: bool | None = None,
    ) -> LLMReply:
        """发一次请求。

        cancel_check 取消时抛错，异常会直接穿过（不吞、不重试）。
        json_mode 按趟覆盖 response_format：顶层要数组的趟（extract）必须关掉
        json_object 模式，否则模型只能把数组包成对象、甚至回显 {"type":"json_object"}。
        """
        _check_cancel(cancel_check)
        if not self.stream:
            return self._request(
                system,
                user,
                max_output_tokens,
                streaming=False,
                cancel_check=cancel_check,
                json_mode=json_mode,
            )
        try:
            return self._request(
                system,
                user,
                max_output_tokens,
                streaming=True,
                cancel_check=cancel_check,
                json_mode=json_mode,
            )
        except _UnsupportedStreaming as exc:
            if "stream_options" in exc.body:
                return self._request(
                    system,
                    user,
                    max_output_tokens,
                    streaming=True,
                    include_usage=False,
                    cancel_check=cancel_check,
                    json_mode=json_mode,
                )
            return self._request(
                system,
                user,
                max_output_tokens,
                streaming=False,
                cancel_check=cancel_check,
                json_mode=json_mode,
            )

    def _request(
        self,
        system: str,
        user: str,
        max_output_tokens: int,
        *,
        streaming: bool,
        include_usage: bool = True,
        cancel_check=None,
        json_mode: bool | None = None,
    ) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_output_tokens,
        }
        if self.json_mode if json_mode is None else json_mode:
            payload["response_format"] = {"type": "json_object"}
        if streaming:
            payload["stream"] = True
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        _check_cancel(cancel_check)
        try:
            with self._client.stream("POST", "chat/completions", json=payload) as resp:
                content_type = resp.headers.get("content-type", "")
                if resp.status_code >= 400:
                    body = _body_text(resp)
                    lowered = body.lower()
                    if streaming and resp.status_code == 400 and "stream" in lowered:
                        raise _UnsupportedStreaming(body)
                    if resp.status_code == 429:
                        raise LLMRateLimit(f"LLM 限流: {body[:200]}", duration_ms=_ms(started))
                    raise LLMError(
                        f"LLM 返回 {resp.status_code}: {body[:200]}", duration_ms=_ms(started)
                    )
                if streaming and "text/event-stream" in content_type:
                    return self._parse_stream(resp, started, max_output_tokens, cancel_check)
                _check_cancel(cancel_check)
                raw = resp.read().decode("utf-8", "replace")
                return self._parse_body(_json(raw, started), started, max_output_tokens)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"LLM 请求超时: {exc}", duration_ms=_ms(started)) from exc
        except httpx.HTTPError as exc:
            raise LLMDisconnected(
                f"LLM 连接被掐断: {type(exc).__name__}: {exc}", duration_ms=_ms(started)
            ) from exc

    def _parse_stream(
        self, resp: httpx.Response, started: float, max_output_tokens: int, cancel_check=None
    ) -> LLMReply:
        """累积 SSE 增量。usage 由 stream_options 的最后一帧带回（网关不支持就没有）。

        每个数据块之间（最多隔 CANCEL_CHECK_INTERVAL）查一次取消：用户点取消后
        立刻抛错，with 块退出时把连接关掉，不再等模型把这一大段生成完。
        """
        parts: list[str] = []
        model = self.model
        usage: dict = {}
        finish = None
        last_check = time.monotonic()
        for line in resp.iter_lines():
            if cancel_check is not None:
                now = time.monotonic()
                if now - last_check >= CANCEL_CHECK_INTERVAL:
                    last_check = now
                    cancel_check()
            if not line:
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
            elif line.startswith("{"):
                data = line.strip()
            else:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk.get("model"), str) and chunk["model"]:
                model = chunk["model"]
            if chunk.get("usage"):
                usage = chunk["usage"] or {}
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
        details = usage.get("completion_tokens_details") or {}
        return self._reply(
            content="".join(parts),
            model=model,
            usage=usage,
            reasoning_tokens=details.get("reasoning_tokens"),
            finish=finish,
            duration_ms=_ms(started),
            max_output_tokens=max_output_tokens,
        )

    def _parse_body(self, data: dict, started: float, max_output_tokens: int) -> LLMReply:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except Exception as exc:  # noqa: BLE001 - 任何结构异常都按协议错误处理
            raise LLMError(f"LLM 响应结构异常: {exc}", duration_ms=_ms(started)) from exc
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return self._reply(
            content=content,
            model=data.get("model") or self.model,
            usage=usage,
            reasoning_tokens=details.get("reasoning_tokens"),
            finish=choice.get("finish_reason"),
            duration_ms=_ms(started),
            max_output_tokens=max_output_tokens,
        )

    def _reply(
        self,
        *,
        content: str | None,
        model: str,
        usage: dict,
        reasoning_tokens,
        finish: str | None,
        duration_ms: int,
        max_output_tokens: int,
    ) -> LLMReply:
        if not (content or "").strip():
            # 最常见的坑：推理型模型把 max_tokens 全花在 reasoning 上，正文是空的。
            # 报清楚原因，别让它伪装成"JSON 解析失败"。
            finish = finish or "unknown"
            hint = "（思考 tokens 占满预算）" if finish == "length" or reasoning_tokens else ""
            raise LLMError(
                f"模型返回空内容：finish_reason={finish}{hint}"
                f"，max_tokens={max_output_tokens}，reasoning_tokens={reasoning_tokens}；"
                "请调大 AB_LLM_MAX_OUTPUT_TOKENS",
                duration_ms=duration_ms,
            )
        return LLMReply(
            text=content or "",
            model=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=reasoning_tokens,
            duration_ms=duration_ms,
        )


def _json(raw: str, started: float) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError as exc:
        raise LLMError(f"LLM 响应不是 JSON: {exc}", duration_ms=_ms(started)) from exc


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
        stream=getattr(settings, "llm_stream", True),
    )
