import json

import httpx
import pytest

from audiobook.config import get_settings
from audiobook.llm.base import LLMError, LLMRateLimit, LLMReply, LLMTimeout
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
