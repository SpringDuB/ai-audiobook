"""整章分析：一次 LLM 调用直出「角色 + 关系 + 每句情感」。"""

from audiobook.analysis.attribution import hints_for_names
from audiobook.analysis.chapter import analyze_chapter, materialize, number_units, windowed
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.text.dialogue import split_units

CHAPTER = "苏锐站在院子里。\n\n“老苏，你怎么看？”王胖子问道。"

CARDS = [
    {
        "name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年",
        "personality": ["冷静"], "speaking_style": "简短", "base_emotion": "平静", "base_intensity": 0.4,
    },
    {
        "name": "王胖子", "aliases": ["胖子"], "gender": "男", "age_group": "青年",
        "personality": ["话多"], "speaking_style": "咋呼", "base_emotion": "喜悦", "base_intensity": 0.5,
    },
    {"name": "旁白", "aliases": [], "gender": "未知", "age_group": "未知", "base_emotion": "平静"},
]

CHARACTERS = {
    "characters": [
        {
            "id": "narrator", "name": "旁白", "aliases": [], "gender": "未知", "age_group": "未知",
            "speaking_style": "平稳", "base_emotion": "平静", "is_narrator": True,
        },
        {
            "id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年",
            "speaking_style": "简短", "base_emotion": "平静",
        },
        {
            "id": "role_0002", "name": "王胖子", "aliases": ["胖子"], "gender": "男", "age_group": "青年",
            "speaking_style": "咋呼", "base_emotion": "喜悦",
        },
    ],
    "relationships": [{"from": "role_0002", "to": "role_0001", "closeness": 0.8, "hierarchy": 0.2}],
}


def _route(user: str) -> dict:
    """假模型：照说话人提示作答，台词给角色、旁白给旁白。"""
    tail = user.split("需要标注的句子：", 1)[-1]
    rows = []
    for line in tail.splitlines():
        if ". " not in line:
            continue
        number, body = line.split(". ", 1)
        if not number.strip().isdigit():
            continue
        speaker = "旁白"
        if "说话人提示：" in body:
            speaker = body.split("说话人提示：", 1)[1].split("]", 1)[0]
        dialogue = "[对白" in body
        rows.append(
            {
                "index": int(number),
                "speaker": speaker,
                "emotion": "惊讶" if dialogue else "平静",
                "intensity": 0.65 if dialogue else 0.3,
                "delivery": "normal",
                "emotion_text": "压着吃惊的口气" if dialogue else "",
            }
        )
    return {"characters": CARDS, "relationships": [{"from": "王胖子", "to": "苏锐", "closeness": 0.8}], "lines": rows}


def _runner(settings, llm):
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings)


def test_windowed_covers_every_unit():
    units = split_units("一二三。四五六。七八九。")
    assert windowed(units, 100) == [(0, 3)]
    # 每句 4 个字，上限 6 时一句都合不进同一窗口
    assert windowed(units, 6) == [(0, 1), (1, 2), (2, 3)]


def test_number_units_marks_kind_and_hint():
    units = split_units("苏锐说：“走。”")
    numbered = number_units(units, hints_for_names(units, ["苏锐"]))
    assert numbered.splitlines() == ["1. [旁白] 苏锐说：", "2. [对白｜说话人提示：苏锐] 走。"]


def test_analyze_chapter_does_characters_and_lines_in_one_call(settings):
    """角色和逐句情感必须同一趟出：一章 = 一次调用（短章不分窗口）。"""
    units = split_units(CHAPTER)
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route})
    result = analyze_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        content=CHAPTER,
        units=units,
        known_names=["苏锐", "王胖子"],
    )
    assert len(llm.calls) == 1
    assert "【CHAPTER_ANALYSIS】" in llm.calls[0]["user"]
    assert [card.name for card in result.analysis.characters] == ["苏锐", "王胖子", "旁白"]
    # 苏锐站在院子里。/ “老苏，你怎么看？” / 王胖子问道。
    assert [line.speaker for line in result.analysis.lines] == ["旁白", "王胖子", "旁白"]
    assert [line.emotion for line in result.analysis.lines] == ["平静", "惊讶", "平静"]
    assert result.issues == []


def test_analyze_chapter_windowed_calls_carry_full_chapter_context(settings):
    tiny = settings.model_copy(update={"llm_line_window_chars": 12})
    units = split_units(CHAPTER)
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route})
    result = analyze_chapter(
        _runner(tiny, llm),
        settings=tiny,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        content=CHAPTER,
        units=units,
        known_names=["苏锐", "王胖子"],
    )
    assert len(llm.calls) >= 2
    # 每个窗口都带上整章全文做上下文，编号接着本章往下数
    assert all("以下是本章全文" in call["user"] for call in llm.calls)
    joined = "\n".join(call["user"] for call in llm.calls)
    assert "2. [对白｜说话人提示：王胖子] 老苏，你怎么看？" in joined
    assert len(result.analysis.lines) == len(units)


def test_window_failure_is_recorded_and_lines_still_materialize(settings):
    units = split_units(CHAPTER)
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route}, fail_on={"CHAPTER_ANALYSIS"})
    result = analyze_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        content=CHAPTER,
        units=units,
    )
    assert [issue["kind"] for issue in result.issues] == ["chapter_window_failed", "line_index_missing"]
    assert result.analysis.lines == []
    # 落盘时不会丢内容：旁白行照旧，引语行用归属句提示兜底
    lines, _ = materialize(
        chapter_index=1,
        units=units,
        annotations=result.analysis.lines,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [line["speaker"] for line in lines] == ["narrator", "role_0002", "narrator"]


def test_materialize_keeps_narrator_and_maps_dialogue(settings):
    units = split_units(CHAPTER)
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route})
    result = analyze_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        content=CHAPTER,
        units=units,
    )
    lines, issues = materialize(
        chapter_index=1,
        units=units,
        annotations=result.analysis.lines,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert issues == []
    assert [line["kind"] for line in lines] == ["narration", "dialogue", "narration"]
    assert [line["speaker"] for line in lines] == ["narrator", "role_0002", "narrator"]
    assert lines[1]["emotion"]["dominant"] == "惊讶"
    assert lines[1]["addressee"] is None
