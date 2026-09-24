from audiobook.analysis.characters import aggregate_characters
from audiobook.analysis.lines import annotate_scene, build_context_block, process_chapter
from audiobook.analysis.models import PassAOutput, PassCOutput
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

SENTENCES = ["秦风看着他。", "“你为什么要杀我？”", "“我说过，会给黄老板一个交代。”"]


def _runner(settings, llm) -> LlmJsonRunner:
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings)


def _characters() -> dict:
    return aggregate_characters(
        [
            (
                1,
                PassAOutput.model_validate(
                    {
                        "characters": [
                            {"name": "秦风", "aliases": ["秦少"], "gender": "男", "base_emotion": "平静"},
                            {"name": "张卫东", "gender": "男", "base_emotion": "恐惧"},
                            {"name": "旁白", "gender": "未知", "base_emotion": "平静"},
                        ],
                        "relationships": [
                            {"from": "张卫东", "to": "秦风", "hostility": 0.7, "intimacy": 0.1}
                        ],
                    }
                ),
            )
        ]
    )


def _scene(index=1, start=1, end=3) -> dict:
    return {
        "id": f"c0001-s{index:02d}",
        "index": index,
        "title": "对峙",
        "summary": "",
        "participants": ["narrator", "role_0001", "role_0002"],
        "tone": {"dominant": "恐惧", "intensity": 0.7},
        "start_line": start,
        "end_line": end,
    }


def test_context_block_only_lists_participants_and_their_relationships():
    payload = _characters()
    relationships = {("role_0002", "role_0001"): payload["relationships"][0]}
    block = build_context_block(payload, _scene()["participants"], relationships)
    assert "秦风" in block and "张卫东" in block
    assert "张卫东 → 秦风" in block
    assert "敌意0.7" in block


def test_annotate_scene_numbers_sentences_and_returns_annotations(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": [{"index": 1, "speaker": "旁白"}]}})
    out = annotate_scene(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        scene=_scene(), sentences=SENTENCES, characters_payload=_characters(), relationships={},
    )
    assert isinstance(out, PassCOutput)
    assert "1. 秦风看着他。" in llm.calls[0]["user"]
    assert "3. “我说过，会给黄老板一个交代。”" in llm.calls[0]["user"]
    assert llm.calls[0]["user"].startswith("【PASS_C】")


def test_process_chapter_assigns_speakers_addressees_and_ids(settings):
    llm = FakeLLM(
        routes={
            "PASS_C": {
                "lines": [
                    {"index": 1, "speaker": "旁白"},
                    {"index": 2, "speaker": "张卫东", "addressee": "秦少", "emotion": "愤怒", "intensity": 0.9,
                     "delivery": "shout"},
                    {"index": 3, "speaker": "秦风", "addressee": "张卫东"},
                ]
            }
        }
    )
    lines, issues = process_chapter(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        sentences=SENTENCES, scenes_payload={"scenes": [_scene()]},
        characters_payload=_characters(), pronounce_table={},
    )
    assert issues == []
    assert [line["id"] for line in lines] == ["c0001-s01-l001", "c0001-s01-l002", "c0001-s01-l003"]
    assert lines[0]["speaker"] == "narrator" and lines[0]["emotion"]["source"] == "scene"
    assert lines[1]["speaker"] == "role_0002" and lines[1]["addressee"] == "role_0001"
    assert lines[1]["emotion"] == {"dominant": "愤怒", "intensity": 0.9, "source": "line"}
    assert lines[1]["addressee_name"] == "秦风"
    assert lines[2]["speaker"] == "role_0001"
    assert lines[2]["emotion"]["source"] == "scene"       # 未标注 → 场景基调
    assert lines[1]["pause_after_ms"] == 500              # 引号后的问号 350 + 句级强度 0.9 ≥ 0.8 追加 150
    assert lines[2]["pause_after_ms"] == 300              # 句号 300；场景强度 0.7 < 0.8 不追加


def test_scene_boundary_pause_attaches_to_last_line_of_previous_scene(settings):
    """场景切换的 500ms 必须落在场景之间，而不是新场景第一句之后。"""
    llm = FakeLLM(
        routes={"PASS_C": {"lines": [{"index": 1, "speaker": "旁白"}, {"index": 2, "speaker": "旁白"}]}}
    )
    scenes = [
        _scene(index=1, start=1, end=1),
        _scene(index=2, start=2, end=2),
    ]
    lines, _ = process_chapter(
        _runner(settings, llm),
        settings=settings,
        book_id="b1",
        chapter_index=1,
        sentences=["第一句。", "第二句。"],
        scenes_payload={"scenes": scenes},
        characters_payload=_characters(),
        pronounce_table={},
    )
    assert lines[0]["pause_after_ms"] == 300 + 500   # 场景 1 最后一句：句号 300 + 切换 500
    assert lines[1]["pause_after_ms"] == 300         # 场景 2 最后一句：句号 300，无切换加成


def test_unknown_speaker_falls_back_to_narrator_and_reports_issue(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": [{"index": 1, "speaker": "黑衣人"}]}})
    lines, issues = process_chapter(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        sentences=["第一句。"], scenes_payload={"scenes": [_scene(start=1, end=1)]},
        characters_payload=_characters(), pronounce_table={},
    )
    assert lines[0]["speaker"] == "narrator"
    assert [issue["kind"] for issue in issues] == ["unknown_speaker"]
    assert issues[0]["line"] == "c0001-s01-l001"


def test_missing_index_is_filled_and_reported(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": [{"index": 1, "speaker": "秦风"}]}})
    lines, issues = process_chapter(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        sentences=SENTENCES[:2], scenes_payload={"scenes": [_scene(start=1, end=2)]},
        characters_payload=_characters(), pronounce_table={},
    )
    assert [line["speaker"] for line in lines] == ["role_0001", "narrator"]
    assert [issue["kind"] for issue in issues] == ["line_index_missing"]
    assert issues[0]["line"] == "c0001-s01-l002"


def test_scene_failure_degrades_to_narrator_with_scene_tone(settings):
    llm = FakeLLM(routes={"PASS_C": {"lines": []}}, fail_on={"PASS_C"})
    lines, issues = process_chapter(
        _runner(settings, llm), settings=settings, book_id="b1", chapter_index=1,
        sentences=SENTENCES, scenes_payload={"scenes": [_scene()]},
        characters_payload=_characters(), pronounce_table={},
    )
    assert all(line["speaker"] == "narrator" for line in lines)
    assert all(line["emotion"]["source"] == "scene" for line in lines)
    assert [issue["kind"] for issue in issues] == ["pass_c_failed"]
    assert issues[0]["detail"] == {"sentences": 3}
