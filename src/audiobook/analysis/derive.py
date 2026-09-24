import re

from ..text.prosody import derive_pause_ms, derive_rate
from .models import DELIVERIES, EMOTIONS, clamp01
from .pronounce import match_pronunciations

KANA = re.compile(r"[\u3040-\u30ff]")
CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")

HOSTILITY_THRESHOLD = 0.6
INTIMACY_THRESHOLD = 0.6


def line_id(chapter_index: int, scene_index: int, seq: int) -> str:
    return f"c{chapter_index:04d}-s{scene_index:02d}-l{seq:03d}"


def scene_id(chapter_index: int, scene_index: int) -> str:
    return f"c{chapter_index:04d}-s{scene_index:02d}"


def derive_lang(text: str) -> str:
    if KANA.search(text):
        return "JP"
    if CJK.search(text):
        return "ZH"
    if LATIN.search(text):
        return "EN"
    return "ZH"


def relationship_emotion(relationship: dict | None) -> tuple[str, float] | None:
    if not relationship:
        return None
    hostility = float(relationship.get("hostility") or 0.0)
    intimacy = float(relationship.get("intimacy") or 0.0)
    if hostility >= HOSTILITY_THRESHOLD:
        return "愤怒", round(hostility, 3)
    if intimacy >= INTIMACY_THRESHOLD:
        return "喜悦", round(intimacy, 3)
    return None


def resolve_emotion(line_emotion, line_intensity, scene_tone, relationship, character) -> dict:
    """四层优先级：句级 > 场景 > 关系 > 角色底色。"""
    if line_emotion in EMOTIONS:
        intensity = 0.6 if line_intensity is None else clamp01(line_intensity)
        return {"dominant": line_emotion, "intensity": round(intensity, 3), "source": "line"}
    if scene_tone and scene_tone.get("dominant") in EMOTIONS:
        return {
            "dominant": scene_tone["dominant"],
            "intensity": round(clamp01(scene_tone.get("intensity", 0.4)), 3),
            "source": "scene",
        }
    from_relationship = relationship_emotion(relationship)
    if from_relationship:
        return {"dominant": from_relationship[0], "intensity": from_relationship[1], "source": "relationship"}
    if character and character.get("base_emotion") in EMOTIONS:
        return {
            "dominant": character["base_emotion"],
            "intensity": round(clamp01(character.get("base_intensity", 0.4)), 3),
            "source": "character",
        }
    return {"dominant": "平静", "intensity": 0.3, "source": "none"}


def derive_line(
    row: dict,
    *,
    chapter_index: int,
    scene_index: int,
    seq: int,
    sentence: str,
    speaker_id: str,
    speaker_name: str,
    addressee_id: str | None,
    addressee_name: str | None,
    scene_tone: dict | None,
    character: dict | None,
    relationship: dict | None,
    pronounce_table: dict[str, str],
    scene_switch: bool = False,
) -> dict:
    emotion = resolve_emotion(row.get("emotion"), row.get("intensity"), scene_tone, relationship, character)
    delivery = row.get("delivery") if row.get("delivery") in DELIVERIES else "normal"
    return {
        "id": line_id(chapter_index, scene_index, seq),
        "scene": scene_id(chapter_index, scene_index),
        "scene_index": scene_index,
        "seq": seq,
        "speaker": speaker_id,
        "speaker_name": speaker_name,
        "addressee": addressee_id,
        "addressee_name": addressee_name,
        "text": sentence,
        "emotion": emotion,
        "delivery": delivery,
        "lang": derive_lang(sentence),
        "pause_after_ms": derive_pause_ms(sentence, scene_switch=scene_switch, intensity=emotion["intensity"]),
        "rate": derive_rate(delivery, emotion["intensity"]),
        "pronounce": match_pronunciations(sentence, pronounce_table),
    }
