"""提取阶段：整章交给大模型，直出「每句话 + 说话人 + 对白情绪」。

这里不测规则分词：分句、归属、前缀剥离全部由模型完成，代码只负责
窗口切分、严格校验、失败兜底与原文完整性检查。
"""

import pytest

from audiobook.analysis.extract import (
    ChapterExtraction,
    dump_extraction,
    extract_chapter,
    is_legacy_extraction,
    load_extraction,
    window_text,
)
from audiobook.analysis.models import SpokenLine
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

CHAPTER = "小鹿：多人？还能自己生成？\n苏锐站在门口。\n“老苏，你怎么看？”王胖子问道。"


def _runner(settings, llm):
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings)


def _route(user: str) -> list[dict]:
    """假模型：照着用户方案里的样例作答（含 "X：" 前缀剥离）。"""
    body = user.split("【正文】", 1)[-1]
    rows: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("小鹿："):
            rows.append({"text": line.split("：", 1)[1], "role": "小鹿", "emotion": "惊讶", "intensity": 0.7})
        elif line.startswith("“"):
            end = line.index("”") + 1
            rows.append({"text": line[:end], "role": "苏锐", "emotion": "平静", "intensity": 0.4})
            tail = line[end:]
            if tail:
                rows.append({"text": tail, "role": "旁白", "emotion": None})
        else:
            rows.append({"text": line, "role": "旁白", "emotion": None})
    return rows


def _extract(settings, llm, content: str = CHAPTER, **overrides):
    kwargs = dict(
        book_id="b1",
        chapter_index=1,
        title="第一章",
        content=content,
        window_chars=settings.llm_line_window_chars,
    )
    kwargs.update(overrides)
    return extract_chapter(_runner(settings, llm), **kwargs)


def test_window_text_keeps_every_character_in_order():
    text = "一。\n二。\n三。"
    windows = window_text(text, 4)
    assert windows == ["一。", "二。", "三。"]
    assert "\n".join(windows) == text
    assert window_text("短句。", 100) == ["短句。"]


def test_extract_chapter_uses_prefix_rule_and_emotion_from_the_model(settings):
    llm = FakeLLM(routes={"【EXTRACT】": _route})
    result = _extract(settings, llm)

    assert len(llm.calls) == 1
    assert "【EXTRACT】" in llm.calls[0]["user"]
    assert [line.role for line in result.lines] == ["小鹿", "旁白", "苏锐", "旁白"]
    # 【强制】"小鹿：" 前缀不进 text，role 必须是 小鹿
    assert result.lines[0].text == "多人？还能自己生成？"
    assert result.lines[0].emotion == "惊讶"
    # 旁白不带情绪（模型给了也会被丢掉）
    assert result.lines[1].emotion is None
    assert result.issues == []


def test_extract_stops_between_windows_when_cancelled(settings):
    """提取途中点取消：下一段不再发请求，异常直接冒泡（不落盘、不兜底成旁白）。"""
    state = {"cancelled": False}

    def route(user: str) -> list[dict]:
        state["cancelled"] = True      # 第一段跑完 = 用户此刻点了取消
        return [{"text": "第一段。", "role": "旁白"}]

    class Cancelled(RuntimeError):
        pass

    def cancel_check():
        if state["cancelled"]:
            raise Cancelled("任务已取消")

    llm = FakeLLM(routes={"【EXTRACT】": route})
    with pytest.raises(Cancelled):
        _extract(
            settings,
            llm,
            content="第一段。\n第二段。",
            window_chars=5,
            cancel_check=cancel_check,
        )
    assert len(llm.calls) == 1


def test_extract_accepts_an_object_wrapped_array(settings):
    """接口强制 JSON 对象时模型会把数组包一层：提取不该因此整段失败。"""

    def route(user: str) -> dict:
        return {
            "type": "json_object",
            "lines": [{"text": "他说。", "role": "旁白", "voice": "平静地陈述，语速中等"}],
        }

    llm = FakeLLM(routes={"【EXTRACT】": route})
    result = _extract(settings, llm, content="他说。")

    assert [line.text for line in result.lines] == ["他说。"]
    assert result.lines[0].voice == "平静地陈述，语速中等"
    assert result.issues == []


def test_extract_turns_off_json_object_mode(settings):
    """顶层要数组的趟不能带 response_format=json_object：网关会逼模型把数组包成对象。"""
    seen = {}

    class RecordingLLM(FakeLLM):
        def complete(self, system, user, *, max_output_tokens=4096, cancel_check=None, json_mode=None):
            seen["json_mode"] = json_mode
            return super().complete(
                system,
                user,
                max_output_tokens=max_output_tokens,
                cancel_check=cancel_check,
                json_mode=json_mode,
            )

    llm = RecordingLLM(routes={"【EXTRACT】": [{"text": "好。", "role": "旁白"}]})
    _extract(settings, llm, content="好。")
    assert seen["json_mode"] is False


def test_extract_chapter_windows_carry_known_names_and_context(settings):
    llm = FakeLLM(routes={"【EXTRACT】": _route})
    result = _extract(settings, llm, window_chars=20, known_names=["小鹿"])

    assert len(llm.calls) > 1
    assert all("【EXTRACT】" in call["user"] for call in llm.calls)
    assert all("小鹿" in call["user"] for call in llm.calls)
    # 段落之间的换行在拆句后不再保留，逐字内容必须一致
    joined = "".join(line.text for line in result.lines).replace("\n", "")
    assert joined == CHAPTER.replace("小鹿：", "").replace("\n", "")
    assert result.issues == []
    assert result.windows == len(llm.calls)


def test_extract_window_failure_keeps_text_as_one_narration_line(settings):
    llm = FakeLLM(routes={"【EXTRACT】": _route}, fail_on={"【EXTRACT】"})
    result = _extract(settings, llm)

    assert [issue["kind"] for issue in result.issues] == ["extract_window_failed"]
    assert [line.role for line in result.lines] == ["旁白"]
    assert result.lines[0].text == CHAPTER


def test_extract_records_text_drift_when_the_model_rewrites(settings):
    llm = FakeLLM(routes={"【EXTRACT】": [{"text": "改写过的句子。", "role": "旁白"}]})
    result = _extract(settings, llm)

    kinds = [issue["kind"] for issue in result.issues]
    assert kinds == ["extract_text_drift"]
    assert result.issues[0]["detail"]["ratio"] < 1.0


def test_dump_and_load_round_trip():
    result = ChapterExtraction(
        lines=[SpokenLine(text="走。", role="苏锐", emotion="愤怒", intensity=0.8)],
        issues=[],
        windows=1,
    )
    payload = dump_extraction(result)
    loaded = load_extraction(payload)
    assert [line.model_dump() for line in loaded] == [line.model_dump() for line in result.lines]


def test_legacy_extraction_is_detected_so_it_gets_reextracted():
    """换 Qwen3-TTS 之前的结果带 emotion、没有 voice：要认出来重新提取。"""
    legacy = {"lines": [{"text": "走。", "role": "苏锐", "emotion": "愤怒", "intensity": 0.8}]}
    assert is_legacy_extraction(legacy) is True

    current = {"lines": [{"text": "走。", "role": "苏锐", "voice": "压低声音，语速偏快"}]}
    assert is_legacy_extraction(current) is False
    # 新格式但模型没写 voice：不能判成老格式，否则每次分析都白重跑一遍
    quiet = {"lines": [{"text": "走。", "role": "苏锐"}]}
    assert is_legacy_extraction(quiet) is False
    assert is_legacy_extraction(None) is False
