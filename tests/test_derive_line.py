import pytest

from audiobook.analysis.derive import (
    derive_lang,
    derive_line,
    line_id,
    relationship_emotion,
    resolve_emotion,
)
from audiobook.analysis.pronounce import load_pronounce_table, match_pronunciations

CHARACTER = {"id": "role_0001", "name": "苏锐", "base_emotion": "平静", "base_intensity": 0.4}


def _base(**overrides) -> dict:
    data = {
        "chapter_index": 1,
        "scene_index": 1,
        "seq": 1,
        "speaker_id": "narrator",
        "speaker_name": "旁白",
        "addressee_id": None,
        "addressee_name": None,
        "character": None,
        "relationship": None,
        "pronounce_table": {},
    }
    data.update(overrides)
    return data


def test_emotion_priority_line_over_inherit_over_relationship_over_character():
    rel = {"hostility": 0.9, "intimacy": 0.0}
    shouted = resolve_emotion("愤怒", 0.8, relationship=rel, character=CHARACTER)
    assert shouted == {
        "dominant": "愤怒",
        "intensity": 0.8,
        "source": "line",
        "mix": [{"name": "愤怒", "weight": 0.8}],
    }
    assert resolve_emotion("继承", None, previous_emotion=shouted, relationship=rel) == {
        "dominant": "愤怒",
        "intensity": 0.8,
        "source": "inherit",
        "mix": shouted["mix"],
    }
    assert resolve_emotion(None, None, relationship=rel, character=CHARACTER) == {
        "dominant": "愤怒",
        "intensity": 0.9,
        "source": "relationship",
    }
    assert resolve_emotion(None, None, character=CHARACTER) == {
        "dominant": "平静",
        "intensity": 0.4,
        "source": "character",
    }
    assert resolve_emotion(None, None) == {"dominant": "平静", "intensity": 0.3, "source": "none"}


def test_secondary_emotion_becomes_part_of_the_mix():
    """两层情绪：表面 + 藏着的那层，合成时一起进 8 维情感向量。"""
    emotion = resolve_emotion("喜悦", 0.6, "愤怒", 0.35)
    assert emotion["mix"] == [{"name": "喜悦", "weight": 0.6}, {"name": "愤怒", "weight": 0.35}]
    # 副情绪有上限（0.5），也不会和主情绪重复
    assert resolve_emotion("喜悦", 0.6, "愤怒", 0.9)["mix"][1]["weight"] == 0.5
    assert resolve_emotion("喜悦", 0.6, "喜悦", 0.4)["mix"] == [{"name": "喜悦", "weight": 0.6}]
    assert resolve_emotion("喜悦", 0.6, "不存在的情绪", 0.4)["mix"] == [{"name": "喜悦", "weight": 0.6}]


def test_relationship_emotion_thresholds():
    assert relationship_emotion({"hostility": 0.6, "intimacy": 0.0}) == ("愤怒", 0.6)
    assert relationship_emotion({"hostility": 0.0, "intimacy": 0.61}) == ("喜悦", 0.61)
    assert relationship_emotion({"hostility": 0.59, "intimacy": 0.59}) is None
    assert relationship_emotion(None) is None


def test_derive_lang_detects_chinese_japanese_english():
    assert derive_lang("他说：你好。") == "ZH"
    assert derive_lang("こんにちは") == "JP"
    assert derive_lang("Hello there") == "EN"
    assert derive_lang("1234 ……") == "ZH"


def test_derive_line_fills_every_contract_field():
    line = derive_line(
        {"emotion": "愤怒", "intensity": 0.9, "secondary": "悲伤", "secondary_weight": 0.3, "delivery": "shout"},
        chapter_index=7, scene_index=2, seq=14, sentence="你重说一遍！",
        speaker_id="role_0001", speaker_name="苏锐", addressee_id="role_0002", addressee_name="王胖子",
        character=CHARACTER, relationship=None,
        pronounce_table={"重": "CHONG2", "行": "XING2"},
    )
    assert line["id"] == "c0007-s02-l014"
    assert line["scene"] == "c0007-s02" and line["scene_index"] == 2 and line["seq"] == 14
    assert line["speaker"] == "role_0001" and line["speaker_name"] == "苏锐"
    assert line["addressee"] == "role_0002" and line["addressee_name"] == "王胖子"
    assert line["emotion"]["dominant"] == "愤怒" and line["emotion"]["source"] == "line"
    assert line["emotion"]["mix"] == [{"name": "愤怒", "weight": 0.9}, {"name": "悲伤", "weight": 0.3}]
    assert line["delivery"] == "shout"
    assert line["lang"] == "ZH"
    assert line["rate"] == pytest.approx(1.113, abs=0.01)  # shout 1.05 × 愤怒 1.06
    assert line["pause_after_ms"] == 500  # 感叹号 350 + 高强度 150
    assert line["pronounce"] == {"重": "CHONG2"}


def test_derive_line_normalizes_unknown_delivery_and_falls_back_to_character():
    line = derive_line(
        {"delivery": "screaming"},
        sentence="随便。",
        **_base(character=CHARACTER),
    )
    assert line["delivery"] == "normal"
    assert line["emotion"]["source"] == "character"
    assert line_id(1, 1, 1) == "c0001-s01-l001"


def test_pause_ignores_trailing_quotes_on_dialogue():
    assert derive_line({"emotion": "平静", "intensity": 0.2}, sentence="“你为什么要杀我？”", **_base())["pause_after_ms"] == 350
    assert derive_line({"emotion": "平静", "intensity": 0.2}, sentence="“我说过。”", **_base())["pause_after_ms"] == 300


def test_pronounce_table_loading_and_matching(settings):
    assert load_pronounce_table(settings) == {}
    path = settings.data_dir / "pronounce.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"重": "CHONG2", "行": "XING2"}', encoding="utf-8")
    table = load_pronounce_table(settings)
    assert match_pronunciations("重来一次", table) == {"重": "CHONG2"}
    assert match_pronunciations("没有命中", table) == {}
