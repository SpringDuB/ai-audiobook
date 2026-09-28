import pytest

from audiobook.analysis.derive import derive_lang, derive_line, line_id, scene_id
from audiobook.analysis.pronounce import load_pronounce_table, match_pronunciations


def _base(**overrides) -> dict:
    data = {
        "chapter_index": 1,
        "scene_index": 1,
        "seq": 1,
        "speaker_id": "narrator",
        "speaker_name": "旁白",
        "kind": "narration",
        "pronounce_table": {},
    }
    data.update(overrides)
    return data


def test_narration_never_carries_an_emotion_vector():
    line = derive_line({"emotion": "悲伤", "intensity": 0.9}, sentence="夜色很深。", **_base())
    assert line["kind"] == "narration"
    assert line["emotion"] == {"dominant": "平静", "intensity": 0.0, "source": "none"}
    assert line["emotion_text"] is None
    assert line["rate"] == 1.0


def test_dialogue_emotion_comes_straight_from_the_model():
    line = derive_line(
        {"emotion": "愤怒", "intensity": 0.9, "secondary": "悲伤", "secondary_weight": 0.3, "delivery": "shout"},
        sentence="你重说一遍！",
        **_base(speaker_id="role_0001", speaker_name="苏锐", kind="dialogue"),
    )
    assert line["id"] == "c0001-s01-l001"
    assert line["scene"] == "c0001-s01" and line["scene_index"] == 1 and line["seq"] == 1
    assert line["speaker"] == "role_0001" and line["speaker_name"] == "苏锐"
    assert line["emotion"]["dominant"] == "愤怒" and line["emotion"]["source"] == "line"
    assert line["emotion"]["mix"] == [{"name": "愤怒", "weight": 0.9}, {"name": "悲伤", "weight": 0.3}]
    assert line["delivery"] == "shout"
    assert line["lang"] == "ZH"
    assert line["rate"] == pytest.approx(1.113, abs=0.01)  # shout 1.05 × 愤怒 1.06
    assert "pause_after_ms" not in line          # 句间停顿机制已删除
    assert line["addressee"] is None


def test_dialogue_without_emotion_falls_back_to_calm_with_default_source():
    line = derive_line({}, sentence="随便。", **_base(kind="dialogue"))
    assert line["emotion"] == {"dominant": "平静", "intensity": 0.5, "source": "default"}


def test_secondary_emotion_is_capped_and_unknown_values_are_dropped():
    line = derive_line(
        {"emotion": "喜悦", "intensity": 0.6, "secondary": "愤怒", "secondary_weight": 0.9},
        sentence="很好。",
        **_base(kind="dialogue"),
    )
    assert line["emotion"]["mix"][1] == {"name": "愤怒", "weight": 0.5}
    same = derive_line(
        {"emotion": "喜悦", "intensity": 0.6, "secondary": "喜悦", "secondary_weight": 0.4},
        sentence="很好。",
        **_base(kind="dialogue"),
    )
    assert same["emotion"]["mix"] == [{"name": "喜悦", "weight": 0.6}]
    bogus = derive_line({"emotion": "不存在的情绪"}, sentence="嗯。", **_base(kind="dialogue"))
    assert bogus["emotion"]["source"] == "default"


def test_derive_lang_detects_chinese_japanese_english():
    assert derive_lang("他说：你好。") == "ZH"
    assert derive_lang("こんにちは") == "JP"
    assert derive_lang("Hello there") == "EN"
    assert derive_lang("1234 ……") == "ZH"


def test_ids_are_stable_and_position_independent():
    assert line_id(7, 1, 14) == "c0007-s01-l014"
    assert scene_id(7, 1) == "c0007-s01"


def test_pronounce_table_loading_and_matching(settings):
    assert load_pronounce_table(settings) == {}
    path = settings.data_dir / "pronounce.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"重": "CHONG2", "行": "XING2"}', encoding="utf-8")
    table = load_pronounce_table(settings)
    assert match_pronunciations("重来一次", table) == {"重": "CHONG2"}
    assert match_pronunciations("没有命中", table) == {}
    line = derive_line(
        {"emotion": "平静", "intensity": 0.3},
        sentence="重来一次。",
        **_base(kind="dialogue", pronounce_table=table),
    )
    assert line["pronounce"] == {"重": "CHONG2"}
