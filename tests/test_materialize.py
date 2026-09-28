"""落盘阶段：把「说话人名字 + 情绪」物化成行记录（id / 音色键 / 停顿 / 语速）。"""

from audiobook.analysis.materialize import materialize
from audiobook.analysis.models import SpokenLine

CHARACTERS = {
    "characters": [
        {"id": "narrator", "name": "旁白", "aliases": [], "is_narrator": True},
        {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "is_narrator": False},
    ]
}


def _spoken(*items) -> list[SpokenLine]:
    return [SpokenLine.model_validate(item) for item in items]


def test_materialize_maps_names_to_role_ids_and_keeps_ids_stable():
    lines, issues = materialize(
        chapter_index=7,
        spoken=_spoken(
            {"text": "苏锐站在门口。", "role": "旁白"},
            {"text": "老苏，你怎么看？", "role": "老苏", "emotion": "惊讶", "intensity": 0.65},
        ),
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert issues == []
    assert [row["id"] for row in lines] == ["c0007-s01-l001", "c0007-s01-l002"]
    assert [row["kind"] for row in lines] == ["narration", "dialogue"]
    assert [row["speaker"] for row in lines] == ["narrator", "role_0001"]
    assert lines[1]["emotion"]["dominant"] == "惊讶"
    assert lines[1]["emotion"]["mix"] == [{"name": "惊讶", "weight": 0.65}]
    assert lines[1]["addressee"] is None


def test_narration_gets_no_emotion_vector_source():
    lines, _ = materialize(
        chapter_index=1,
        spoken=_spoken({"text": "夜色很深。", "role": "旁白", "emotion": "悲伤", "intensity": 0.9}),
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert lines[0]["kind"] == "narration"
    assert lines[0]["emotion"] == {"dominant": "平静", "intensity": 0.0, "source": "none"}
    assert lines[0]["emotion_text"] is None
    assert lines[0]["rate"] == 1.0


def test_unknown_role_falls_back_to_narrator_and_is_visible():
    lines, issues = materialize(
        chapter_index=1,
        spoken=_spoken(
            {"text": "谁？", "role": "黑衣人", "emotion": "恐惧", "intensity": 0.7},
            {"text": "别过来。", "role": "未知", "emotion": "恐惧"},
        ),
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [row["speaker"] for row in lines] == ["narrator", "narrator"]
    assert [row["kind"] for row in lines] == ["dialogue", "dialogue"]
    kinds = [issue["kind"] for issue in issues]
    assert kinds == ["unknown_speaker"]
    assert issues[0]["detail"] == {"count": 2, "roles": ["黑衣人", "未知"]}
    assert issues[0]["fallback"] == "按旁白音色处理"


def test_dialogue_without_emotion_is_flagged_and_defaults_to_calm():
    lines, issues = materialize(
        chapter_index=1,
        spoken=_spoken({"text": "随便吧。", "role": "苏锐", "emotion": None}),
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert lines[0]["emotion"]["dominant"] == "平静"
    assert lines[0]["emotion"]["source"] == "default"
    assert [issue["kind"] for issue in issues] == ["emotion_missing"]
    assert issues[0]["detail"]["count"] == 1


def test_materialize_applies_pronunciation_table():
    lines, _ = materialize(
        chapter_index=1,
        spoken=_spoken({"text": "重来一次。", "role": "老苏", "emotion": "平静", "intensity": 0.3}),
        characters_payload=CHARACTERS,
        pronounce_table={"重": "CHONG2"},
    )
    assert lines[0]["pronounce"] == {"重": "CHONG2"}
