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


def resolve_emotion(
    line_emotion,
    line_intensity,
    secondary=None,
    secondary_weight=None,
    previous_emotion=None,
    relationship=None,
    character=None,
) -> dict:
    """优先级：句级 > 上一句（模型写"继承"时）> 人物关系 > 角色底色。

    句级结果带 mix：主情绪 + 可选的副情绪（同一句话里的第二层情绪），
    合成时按这两个权重拼出 IndexTTS 的 8 维情感向量。
    """
    if line_emotion in EMOTIONS:
        intensity = 0.6 if line_intensity is None else clamp01(line_intensity)
        mix = [{"name": line_emotion, "weight": round(intensity, 3)}]
        if secondary in EMOTIONS and secondary != line_emotion:
            weight = clamp01(0.3 if secondary_weight is None else secondary_weight)
            if weight > 0:
                mix.append({"name": secondary, "weight": round(min(weight, 0.5), 3)})
        return {"dominant": line_emotion, "intensity": round(intensity, 3), "source": "line", "mix": mix}
    if previous_emotion and previous_emotion.get("dominant") in EMOTIONS:
        inherited = {
            "dominant": previous_emotion["dominant"],
            "intensity": round(clamp01(previous_emotion.get("intensity", 0.4)), 3),
            "source": "inherit",
        }
        if previous_emotion.get("mix"):
            inherited["mix"] = previous_emotion["mix"]
        return inherited
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
    character: dict | None,
    relationship: dict | None,
    pronounce_table: dict[str, str],
    previous_emotion: dict | None = None,
) -> dict:
    emotion = resolve_emotion(
        row.get("emotion"),
        row.get("intensity"),
        row.get("secondary"),
        row.get("secondary_weight"),
        previous_emotion,
        relationship,
        character,
    )
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
        "pause_after_ms": derive_pause_ms(sentence, intensity=emotion["intensity"]),
        "rate": derive_rate(delivery, emotion),
        "pronounce": match_pronunciations(sentence, pronounce_table),
    }
