from audiobook.analysis.lines import (
    DEFAULT_SEGMENT,
    build_context_block,
    participants_in_window,
    process_chapter,
    windowed,
)
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

SENTENCES = ["苏锐说：“走！”", "他停了下来。", "苏锐又说：“别废话。”"]

CHARACTERS = {
    "characters": [
        {
            "id": "narrator",
            "name": "旁白",
            "aliases": [],
            "gender": "未知",
            "age_group": "未知",
            "personality": [],
            "speaking_style": "平稳",
            "base_emotion": "平静",
            "base_intensity": 0.3,
            "is_narrator": True,
        },
        {
            "id": "role_0001",
            "name": "苏锐",
            "aliases": ["老苏"],
            "gender": "男",
            "age_group": "青年",
            "personality": ["冷静"],
            "speaking_style": "简短",
            "base_emotion": "平静",
            "base_intensity": 0.4,
            "is_narrator": False,
        },
        {
            "id": "role_0002",
            "name": "王胖子",
            "aliases": [],
            "gender": "男",
            "age_group": "青年",
            "personality": ["话多"],
            "speaking_style": "咋呼",
            "base_emotion": "喜悦",
            "base_intensity": 0.5,
            "is_narrator": False,
        },
    ],
    "relationships": [
        {"from": "苏锐", "to": "王胖子", "closeness": 0.8, "hierarchy": 0.2, "hostility": 0.0, "intimacy": 0.7}
    ],
}


def _runner(settings, llm):
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings)


def _route_c(user: str) -> dict:
    tail = user.split("句子列表：", 1)[-1]
    rows = []
    for line in tail.splitlines():
        if ". " not in line:
            continue
        number, sentence = line.split(". ", 1)
        if not number.strip().isdigit():
            continue
        quoted = "“" in sentence
        rows.append(
            {
                "index": int(number),
                "speaker": "苏锐" if "苏锐" in sentence else "旁白",
                "addressee": "王胖子" if "苏锐" in sentence else None,
                "emotion": "愤怒" if quoted else "忧郁",
                "intensity": 0.8 if quoted else 0.35,
                "secondary": "悲伤" if quoted else None,
                "secondary_weight": 0.3 if quoted else 0.0,
                "delivery": "shout" if "！" in sentence else "normal",
            }
        )
    return {"lines": rows}


def test_windowed_covers_every_sentence():
    assert windowed(["一二三", "四五六", "七八九"], 100) == [(0, 3)]
    assert windowed(["一二三", "四五六", "七八九"], 6) == [(0, 2), (2, 3)]
    # 句数上限兜住"短句很多"的情况（否则一条记录几十 token，输出会被截断）
    assert windowed(["短。"] * 5, 10_000, max_sentences=2) == [(0, 2), (2, 4), (4, 5)]


def test_participants_in_window_are_mentioned_characters_plus_narrator():
    assert participants_in_window(["苏锐看着他。"], CHARACTERS) == ["narrator", "role_0001"]
    # 别名也算命中
    assert participants_in_window(["老苏笑了。"], CHARACTERS) == ["narrator", "role_0001"]
    assert participants_in_window(["天亮了。"], CHARACTERS) == ["narrator"]


def test_context_block_lists_characters_and_their_relationship():
    block = build_context_block(CHARACTERS, ["narrator", "role_0001", "role_0002"], {("role_0001", "role_0002"): {
        "closeness": 0.8, "hierarchy": 0.2, "hostility": 0.0, "intimacy": 0.7
    }})
    assert "苏锐（男，青年，底色：平静，说话习惯：简短）" in block
    assert "苏锐 → 王胖子：亲疏0.8、尊卑0.2、敌意0.0、亲密0.7" in block


def test_process_chapter_keeps_absolute_numbers_and_mix(settings):
    tiny = settings.model_copy(update={"llm_line_window_chars": 10})
    llm = FakeLLM(routes={"PASS_C": _route_c})
    lines, issues = process_chapter(
        _runner(settings, llm),
        settings=tiny,
        book_id="b1",
        chapter_index=1,
        title="第一章 出发",
        sentences=SENTENCES,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert issues == []
    # 行 id 用"本章第几句"，跨窗口连续
    assert [line["id"] for line in lines] == ["c0001-s01-l001", "c0001-s01-l002", "c0001-s01-l003"]
    assert [line["seq"] for line in lines] == [1, 2, 3]
    assert all(line["scene_index"] == DEFAULT_SEGMENT for line in lines)
    assert [line["speaker"] for line in lines] == ["role_0001", "narrator", "role_0001"]
    assert lines[0]["addressee"] == "role_0002"
    assert lines[0]["emotion"] == {
        "dominant": "愤怒",
        "intensity": 0.8,
        "source": "line",
        "mix": [{"name": "愤怒", "weight": 0.8}, {"name": "悲伤", "weight": 0.3}],
    }
    assert lines[1]["emotion"]["dominant"] == "忧郁" and lines[1]["emotion"]["source"] == "line"
    assert lines[0]["delivery"] == "shout"
    # 第二个窗口的编号必须接着本章往下数
    assert len(llm.calls) >= 2
    assert "2. 他停了下来。" in llm.calls[1]["user"]
    assert "第一章 出发" in llm.calls[0]["user"]


def test_missing_annotation_is_reported_and_inherits(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": [
        {"index": 1, "speaker": "苏锐", "emotion": "愤怒", "intensity": 0.9, "delivery": "shout"},
        {"index": 3, "speaker": "苏锐", "emotion": "平静", "intensity": 0.3},
    ]}})
    lines, issues = process_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        sentences=SENTENCES,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [issue["kind"] for issue in issues] == ["line_index_missing"]
    assert lines[1]["emotion"] == {
        "dominant": "愤怒",
        "intensity": 0.9,
        "source": "inherit",
        "mix": [{"name": "愤怒", "weight": 0.9}],
    }


def test_window_failure_degrades_to_narrator_and_records_issue(settings, tmp_path):
    llm = FakeLLM(routes={"PASS_C": _route_c}, fail_on={"PASS_C"})
    lines, issues = process_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        sentences=SENTENCES,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [issue["kind"] for issue in issues] == ["pass_c_failed"]
    assert all(line["speaker"] == "narrator" for line in lines)
    assert len(lines) == len(SENTENCES)


def test_unknown_speaker_is_recorded_and_treated_as_narrator(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": [
        {"index": index, "speaker": "黑衣人", "emotion": "平静"} for index in range(1, len(SENTENCES) + 1)
    ]}})
    lines, issues = process_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        title="第一章",
        sentences=SENTENCES,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [issue["kind"] for issue in issues] == ["unknown_speaker"] * len(SENTENCES)
    assert all(line["speaker"] == "narrator" for line in lines)
