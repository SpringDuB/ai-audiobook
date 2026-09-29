import json

import httpx
import pytest

from audiobook.config import get_settings
from audiobook.llm.base import LLMDisconnected, LLMError, LLMRateLimit, LLMReply, LLMTimeout
from audiobook.llm.fake import FakeLLM
from audiobook.llm.openai_compat import OpenAICompatClient, build_client


def _client(handler, **kwargs) -> OpenAICompatClient:
    base = dict(base_url="http://llm.local/v1", api_key="sk-test", model="deepseek-flash")
    base.update(kwargs)
    return OpenAICompatClient(transport=httpx.MockTransport(handler), **base)


def test_posts_openai_payload_and_parses_reply():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            },
        )

    reply = _client(handler).complete("系统", "用户")

    assert seen["url"] == "http://llm.local/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "系统"}
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["max_tokens"] == 4096
    assert reply.text == '{"ok":true}'
    assert reply.input_tokens == 11 and reply.output_tokens == 22
    assert reply.model == "deepseek-flash"


def test_json_mode_can_be_disabled():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _client(handler, json_mode=False).complete("s", "u")
    assert "response_format" not in seen["body"]


def _sse(chunks: list[dict]) -> bytes:
    text = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (text + "data: [DONE]\n\n").encode("utf-8")


def test_streaming_reply_is_accumulated_with_usage():
    """默认走 SSE：网关按空闲超时掐长请求，流式才能把连接养住。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                [
                    {"model": "deepseek-flash", "choices": [{"delta": {"content": '{"ok":'}}]},
                    {"model": "deepseek-flash", "choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}]},
                    {
                        "model": "deepseek-flash",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 31,
                            "completion_tokens": 42,
                            "completion_tokens_details": {"reasoning_tokens": 17},
                        },
                    },
                ]
            ),
        )

    reply = _client(handler).complete("s", "u")

    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert reply.text == '{"ok":true}'
    assert reply.input_tokens == 31 and reply.output_tokens == 42 and reply.reasoning_tokens == 17


def test_stream_options_rejection_falls_back_to_plain_stream():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unknown parameter: stream_options"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse([{"choices": [{"delta": {"content": "{}"}}]}]),
        )

    reply = _client(handler).complete("s", "u")

    assert reply.text == "{}"
    assert len(seen) == 2
    assert seen[1]["stream"] is True and "stream_options" not in seen[1]


def test_stream_rejection_falls_back_to_plain_json():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream is not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    reply = _client(handler).complete("s", "u")

    assert reply.text == "{}"
    assert "stream" not in seen[-1] and "stream_options" not in seen[-1]


def test_disconnect_is_reported_as_llm_disconnected_with_duration():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    with pytest.raises(LLMDisconnected) as excinfo:
        _client(handler).complete("s", "u")

    assert "RemoteProtocolError" in str(excinfo.value)
    assert excinfo.value.duration_ms is not None


def test_reasoning_tokens_are_reported():
    """推理型模型把思考 tokens 单独记账：日志里要能看到，方便判断预算够不够。"""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 2000,
                    "completion_tokens_details": {"reasoning_tokens": 1800},
                },
            },
        )

    reply = _client(handler).complete("s", "u")
    assert reply.reasoning_tokens == 1800
    assert reply.output_tokens == 2000


def test_empty_content_raises_a_readable_error_instead_of_a_json_error():
    """回归：思考 tokens 吃光 max_tokens 时正文为空，不能伪装成"JSON 解析失败"。"""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 3000,
                    "completion_tokens": 4096,
                    "completion_tokens_details": {"reasoning_tokens": 4096},
                },
            },
        )

    with pytest.raises(LLMError) as excinfo:
        _client(handler).complete("s", "u", max_output_tokens=4096)
    message = str(excinfo.value)
    assert "空内容" in message
    assert "finish_reason=length" in message
    assert "max_tokens=4096" in message
    assert "reasoning_tokens=4096" in message


def test_rate_limit_timeout_and_bad_status_raise_typed_errors():
    def rate_limited(request):
        return httpx.Response(429, text="too many requests")

    with pytest.raises(LLMRateLimit):
        _client(rate_limited).complete("s", "u")

    def broken(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMError):
        _client(broken).complete("s", "u")

    def exploding(request):
        raise httpx.ConnectTimeout("超时")

    with pytest.raises(LLMTimeout):
        _client(exploding).complete("s", "u")


def test_build_client_requires_base_url():
    settings = get_settings(llm_base_url="")
    with pytest.raises(LLMError):
        build_client(settings)


def test_build_client_reads_settings():
    settings = get_settings()
    client = build_client(settings)
    assert client.model == settings.llm_model


def test_fake_llm_routes_by_marker_and_records_calls():
    llm = FakeLLM(routes={"PASS_A": {"characters": []}})
    reply = llm.complete("系统", "【PASS_A】第一章正文")
    assert isinstance(reply, LLMReply)
    assert json.loads(reply.text) == {"characters": []}
    assert llm.calls[0]["user"].startswith("【PASS_A】")


def test_fake_llm_supports_callable_routes_and_injected_failures():
    llm = FakeLLM(routes={"PASS_C": lambda user: {"lines": [{"index": 1, "speaker": "旁白"}]}})
    reply = llm.complete("s", "【PASS_C】1. 第一句。")
    assert json.loads(reply.text)["lines"][0]["speaker"] == "旁白"

    boom = FakeLLM(routes={"PASS_A": {"characters": []}}, fail_on={"PASS_A"})
    with pytest.raises(LLMError):
        boom.complete("s", "【PASS_A】正文")

    limited = FakeLLM(routes={"PASS_A": {"characters": []}}, rate_limit_on={"PASS_A"})
    with pytest.raises(LLMRateLimit):
        limited.complete("s", "【PASS_A】正文")
