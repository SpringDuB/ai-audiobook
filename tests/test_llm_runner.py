import pytest
from pydantic import BaseModel, ConfigDict

from audiobook import store
from audiobook.llm.base import LLMDisconnected, LLMReply
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonError, LlmJsonRunner, clean_json_text


class Demo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    count: int = 0


def _runner(settings, llm, **kwargs) -> LlmJsonRunner:
    limiter = AdaptiveLimiter(max_concurrency=settings.llm_concurrency)
    return LlmJsonRunner(llm, limiter, settings, **kwargs)


def test_returns_model_and_logs_successful_call(settings):
    llm = FakeLLM(routes={"PASS_A": {"ok": True, "count": 3}})
    runner = _runner(settings, llm)

    result = runner.run(
        system="s", user="【PASS_A】正文", model_cls=Demo, pass_name="A",
        book_id="b1", chapter_index=1,
    )

    assert isinstance(result, Demo) and result.count == 3
    rows = store.read_jsonl(store.llm_log_path(settings, "b1"))
    assert len(rows) == 1
    assert rows[0]["pass"] == "A" and rows[0]["chapter"] == 1
    assert rows[0]["ok"] is True and rows[0]["error"] is None
    assert rows[0]["model"] == "fake-llm"


def test_cancel_check_stops_before_the_request_and_is_not_a_failure(settings):
    """用户取消：请求不发、不记失败日志、异常原样抛给 worker（不重试）。"""

    class Cancelled(RuntimeError):
        pass

    llm = FakeLLM(routes={"PASS_C": {"ok": True}})
    runner = _runner(settings, llm)

    def cancel_check():
        raise Cancelled("任务已取消")

    with pytest.raises(Cancelled):
        runner.run(
            system="s",
            user="【PASS_C】正文",
            model_cls=Demo,
            pass_name="C",
            book_id="b1",
            cancel_check=cancel_check,
        )
    assert llm.calls == []
    assert not store.llm_log_path(settings, "b1").exists()


def test_runner_strips_code_fences_and_prose_before_validation(settings):
    """模型爱把 JSON 包在 ```json 里或在前面写两句解释：剥掉再校验，别整段重跑。"""

    class Fenced:
        def complete(self, system, user, *, max_output_tokens=4096, cancel_check=None, json_mode=None):
            return LLMReply(text='好的，结果如下：\n```json\n{"ok": true, "count": 2}\n```\n希望有帮助。', model="fake")

    result = _runner(settings, Fenced()).run(
        system="s", user="u", model_cls=Demo, pass_name="D", book_id="b1"
    )
    assert result.ok is True and result.count == 2
    assert clean_json_text('[{"a":1}]') == '[{"a":1}]'
    assert clean_json_text('```json\n{"ok": true}\n```') == '{"ok": true}'


def test_runner_passes_json_mode_override_to_the_client(settings, monkeypatch):
    """按趟覆盖 response_format：extract 这趟要关掉 json_object。"""
    seen = {}

    class Recording:
        def complete(self, system, user, *, max_output_tokens=4096, cancel_check=None, json_mode=None):
            seen["json_mode"] = json_mode
            return LLMReply(text='{"ok": true}', model="fake")

    _runner(settings, Recording()).run(
        system="s", user="u", model_cls=Demo, pass_name="E", book_id="b1", json_mode=False
    )
    assert seen["json_mode"] is False


def test_retries_once_with_repair_hint_when_json_is_invalid(settings):
    calls = {"n": 0}

    def route(user: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": "不是布尔值"}
        return {"ok": True}

    llm = FakeLLM(routes={"PASS_B": route})
    runner = _runner(settings, llm)

    assert runner.run(system="s", user="【PASS_B】正文", model_cls=Demo, pass_name="B", book_id="b1").ok is True
    assert len(llm.calls) == 2
    assert "上一次输出不是合法 JSON" in llm.calls[1]["user"]
    rows = store.read_jsonl(store.llm_log_path(settings, "b1"))
    assert [r["ok"] for r in rows] == [False, True]
    assert rows[0]["error"].startswith("JSON 校验失败")


def test_raises_after_max_attempts_and_logs_every_failure(settings):
    llm = FakeLLM(routes={"PASS_C": "这不是 JSON"})
    runner = _runner(settings, llm, max_attempts=2)

    with pytest.raises(LlmJsonError) as excinfo:
        runner.run(system="s", user="【PASS_C】正文", model_cls=Demo, pass_name="C", book_id="b2")

    assert excinfo.value.attempts == 2
    assert len(llm.calls) == 2
    rows = store.read_jsonl(store.llm_log_path(settings, "b2"))
    assert [r["ok"] for r in rows] == [False, False]


def test_rate_limit_keeps_concurrency_and_still_raises(settings):
    llm = FakeLLM(routes={"PASS_A": {"ok": True}}, rate_limit_on={"PASS_A"})
    limiter = AdaptiveLimiter(max_concurrency=8, cooldown_seconds=0.0)
    runner = LlmJsonRunner(llm, limiter, settings, max_attempts=1)

    with pytest.raises(LlmJsonError):
        runner.run(system="s", user="【PASS_A】正文", model_cls=Demo, pass_name="A", book_id="b3")

    # 限流只重试，绝不降并发
    assert limiter.limit == 8
    assert limiter.stats()["rate_limits"] == 1
    rows = store.read_jsonl(store.llm_log_path(settings, "b3"))
    assert "LLMRateLimit" in rows[0]["error"]


class _Disconnected:
    """每次都像网关掐连接那样失败。"""

    def __init__(self, duration_ms: int = 60123):
        self.duration_ms = duration_ms
        self.calls = 0

    def complete(self, system: str, user: str, *, max_output_tokens: int = 4096, cancel_check=None):
        self.calls += 1
        raise LLMDisconnected("LLM 连接被掐断: Server disconnected", duration_ms=self.duration_ms)


def test_disconnect_keeps_limit_and_logs_duration(settings):
    llm = _Disconnected()
    limiter = AdaptiveLimiter(max_concurrency=8, cooldown_seconds=0.0, soft_cooldown_seconds=0.0)
    runner = LlmJsonRunner(llm, limiter, settings, max_attempts=1)

    with pytest.raises(LlmJsonError):
        runner.run(system="s", user="【PASS_D】正文", model_cls=Demo, pass_name="D", book_id="b4")

    # 断连只计数重试，不把整个池子的并发打下来
    assert limiter.limit == 8
    assert limiter.stats()["transport_failures"] == 1
    row = store.read_jsonl(store.llm_log_path(settings, "b4"))[0]
    assert "LLMDisconnected" in row["error"]
    assert row["duration_ms"] == 60123  # 失败也要记时长，别再是 0ms


def test_repeated_disconnects_never_downgrade(settings, monkeypatch):
    """10 路并发同时被掐线时，连续失败也不能把并发打到 1（用户踩过的坑）。"""
    monkeypatch.setattr("audiobook.llm.runner.time.sleep", lambda seconds: None)
    llm = _Disconnected()
    limiter = AdaptiveLimiter(
        max_concurrency=8, cooldown_seconds=0.0, soft_cooldown_seconds=0.0, soft_escalate_after=3
    )
    runner = LlmJsonRunner(llm, limiter, settings, max_attempts=9)

    with pytest.raises(LlmJsonError):
        runner.run(system="s", user="【PASS_E】正文", model_cls=Demo, pass_name="E", book_id="b5")

    assert limiter.limit == 8
    assert limiter.stats()["transport_failures"] == 9


def test_disconnect_retries_with_jittered_backoff(settings, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("audiobook.llm.runner.time.sleep", lambda seconds: slept.append(seconds))
    llm = _Disconnected()
    limiter = AdaptiveLimiter(max_concurrency=1, cooldown_seconds=0.0)
    runner = LlmJsonRunner(llm, limiter, settings, max_attempts=3)

    with pytest.raises(LlmJsonError):
        runner.run(system="s", user="【PASS_E】正文", model_cls=Demo, pass_name="E", book_id="b5")

    assert llm.calls == 3
    assert len(slept) == 2  # 最后一次失败后不再等
    assert all(0 < delay <= 8 for delay in slept)
