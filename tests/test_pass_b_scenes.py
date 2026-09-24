from audiobook.analysis.characters import aggregate_characters
from audiobook.analysis.models import PassAOutput, SceneSpan
from audiobook.analysis.scenes import locate_spans, number_sentences, scene_id, split_scenes, windowed
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

SENTENCES = ["第一句。", "第二句。", "第三句。", "第四句。", "第五句。", "第六句。"]


def _runner(settings, llm) -> LlmJsonRunner:
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings)


def _sentences_from_prompt(user: str) -> list[str]:
    """只从 prompt 末尾的"句子列表："里取句子，忽略规则文本里的编号行。"""
    tail = user.split("句子列表：", 1)[-1]
    return [line.split(". ", 1)[1] for line in tail.splitlines() if ". " in line and line[:1].isdigit()]


def _characters() -> dict:
    return aggregate_characters(
        [
            (
                1,
                PassAOutput.model_validate(
                    {
                        "characters": [
                            {"name": "苏锐", "gender": "男", "base_emotion": "平静", "base_intensity": 0.4},
                            {"name": "旁白", "gender": "未知", "base_emotion": "平静", "base_intensity": 0.3},
                        ],
                        "relationships": [],
                    }
                ),
            )
        ]
    )


def test_windowed_keeps_every_sentence_once():
    windows = windowed(SENTENCES, max_chars=6)
    assert windows == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
    assert windowed(SENTENCES, max_chars=1000) == [(0, 6)]


def test_number_sentences_prefixes_line_numbers():
    assert number_sentences(["甲。", "乙。"], start=3) == "3. 甲。\n4. 乙。"


def test_scene_id_uses_chapter_and_scene_numbers():
    assert scene_id(1, 2) == "c0001-s02"
    assert scene_id(37, 10) == "c0037-s10"


def test_locate_spans_uses_model_hints():
    spans = [
        SceneSpan.model_validate({"index": 1, "title": "开场", "starts_with": "第一句。", "ends_with": "第三句。"}),
        SceneSpan.model_validate({"index": 2, "title": "转折", "starts_with": "第四句。", "ends_with": "第六句。"}),
    ]
    scenes, issue = locate_spans(SENTENCES, spans, offset=0, name_to_id={"narrator": "narrator"})
    assert issue is None
    assert [(scene["start_line"], scene["end_line"]) for scene in scenes] == [(1, 3), (4, 6)]
    assert [scene["title"] for scene in scenes] == ["开场", "转折"]


def test_locate_spans_falls_back_to_even_split_and_reports_issue():
    spans = [
        SceneSpan.model_validate({"index": 1, "starts_with": "不存在的句子", "ends_with": ""}),
        SceneSpan.model_validate({"index": 2, "starts_with": "第四句。", "ends_with": "第六句。"}),
    ]
    scenes, issue = locate_spans(SENTENCES, spans, offset=0, name_to_id={})
    assert [(scene["start_line"], scene["end_line"]) for scene in scenes] == [(1, 3), (4, 6)]
    assert issue is not None and issue["kind"] == "scene_hint_not_found"
    assert issue["detail"]["sentences"] == 6


def test_split_scenes_degrades_to_single_scene_when_llm_fails(settings):
    llm = FakeLLM(routes={"PASS_B": {"scenes": []}}, fail_on={"PASS_B"})
    payload, issues = split_scenes(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        title="第一章", sentences=SENTENCES, characters_payload=_characters(),
    )
    assert len(payload["scenes"]) == 1
    scene = payload["scenes"][0]
    assert scene["id"] == "c0001-s01"
    assert (scene["start_line"], scene["end_line"]) == (1, 6)
    assert scene["tone"]["dominant"] == "平静"
    assert [issue["kind"] for issue in issues] == ["pass_b_failed"]
    assert payload["sentence_count"] == 6


def test_split_scenes_renumbers_ids_across_windows(settings):
    small = settings.model_copy(update={"llm_scene_window_chars": 12})

    def route(user: str) -> dict:
        texts = _sentences_from_prompt(user)
        half = len(texts) // 2
        return {
            "scenes": [
                {"index": 1, "title": "A", "starts_with": texts[0], "ends_with": texts[half - 1]},
                {"index": 2, "title": "B", "starts_with": texts[half], "ends_with": texts[-1]},
            ]
        }

    llm = FakeLLM(routes={"PASS_B": route})
    payload, issues = split_scenes(
        _runner(small, llm), settings=small, book_id="b1", chapter_index=1,
        title="第一章", sentences=SENTENCES, characters_payload=_characters(),
    )
    assert [scene["id"] for scene in payload["scenes"]] == ["c0001-s01", "c0001-s02", "c0001-s03", "c0001-s04"]
    assert [(scene["start_line"], scene["end_line"]) for scene in payload["scenes"]] == [(1, 1), (2, 3), (4, 4), (5, 6)]
    assert issues == []
    assert len(llm.calls) == 2
