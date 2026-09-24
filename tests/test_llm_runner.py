import pytest
from pydantic import BaseModel, ConfigDict

from audiobook import store
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonError, LlmJsonRunner


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


def test_rate_limit_lowers_limiter_and_still_raises(settings):
    llm = FakeLLM(routes={"PASS_A": {"ok": True}}, rate_limit_on={"PASS_A"})
    limiter = AdaptiveLimiter(max_concurrency=8, cooldown_seconds=0.0)
    runner = LlmJsonRunner(llm, limiter, settings, max_attempts=1)

    with pytest.raises(LlmJsonError):
        runner.run(system="s", user="【PASS_A】正文", model_cls=Demo, pass_name="A", book_id="b3")

    assert limiter.limit == 4
    rows = store.read_jsonl(store.llm_log_path(settings, "b3"))
    assert "LLMRateLimit" in rows[0]["error"]
