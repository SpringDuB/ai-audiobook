from audiobook.analysis.derive import (
    derive_lang,
    derive_line,
    line_id,
    relationship_emotion,
    resolve_emotion,
)
from audiobook.analysis.pronounce import load_pronounce_table, match_pronunciations

SCENE_TONE = {"dominant": "恐惧", "intensity": 0.7}
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
        "scene_tone": None,
        "character": None,
        "relationship": None,
        "pronounce_table": {},
    }
    data.update(overrides)
    return data


def test_emotion_priority_line_over_scene_over_relationship_over_character():
    rel = {"hostility": 0.9, "intimacy": 0.0}
    assert resolve_emotion("愤怒", 0.8, SCENE_TONE, rel, CHARACTER)["source"] == "line"
    assert resolve_emotion("继承", None, SCENE_TONE, rel, CHARACTER) == {
        "dominant": "恐惧",
        "intensity": 0.7,
        "source": "scene",
    }
    assert resolve_emotion(None, None, None, rel, CHARACTER) == {
        "dominant": "愤怒",
        "intensity": 0.9,
        "source": "relationship",
    }
    assert resolve_emotion(None, None, None, None, CHARACTER) == {
        "dominant": "平静",
        "intensity": 0.4,
        "source": "character",
    }
    assert resolve_emotion(None, None, None, None, None) == {
        "dominant": "平静",
        "intensity": 0.3,
        "source": "none",
    }


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
        {"emotion": "愤怒", "intensity": 0.9, "delivery": "shout"},
        chapter_index=7, scene_index=2, seq=14, sentence="你重说一遍！",
        speaker_id="role_0001", speaker_name="苏锐", addressee_id="role_0002", addressee_name="王胖子",
        scene_tone=SCENE_TONE, character=CHARACTER, relationship=None,
        pronounce_table={"重": "CHONG2", "行": "XING2"}, scene_switch=True,
    )
    assert line["id"] == "c0007-s02-l014"
    assert line["scene"] == "c0007-s02" and line["scene_index"] == 2 and line["seq"] == 14
    assert line["speaker"] == "role_0001" and line["speaker_name"] == "苏锐"
    assert line["addressee"] == "role_0002" and line["addressee_name"] == "王胖子"
    assert line["emotion"] == {"dominant": "愤怒", "intensity": 0.9, "source": "line"}
    assert line["delivery"] == "shout"
    assert line["lang"] == "ZH"
    assert line["rate"] == 1.05  # shout 的固定倍率
    assert line["pause_after_ms"] == 350 + 500 + 150  # 感叹号 + 场景切换 + 高强度
    assert line["pronounce"] == {"重": "CHONG2"}


def test_derive_line_normalizes_unknown_delivery():
    line = derive_line(
        {"delivery": "screaming"},
        sentence="随便。",
        **_base(scene_tone=SCENE_TONE),
    )
    assert line["delivery"] == "normal"
    assert line["emotion"]["source"] == "scene"
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
