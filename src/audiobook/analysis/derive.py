"""把一行标注推导成落盘记录：id / 语速 / 语言 / 注音。

情绪完全来自提取阶段的大模型结果（主情绪 + 可选副情绪），这里不再有
规则推断的情绪层；唯一的约定是：**旁白不带情绪向量**，只有人物话术带。
句与句之间不再插入额外静音：停顿由 TTS 模型自己按文本处理。
"""

import re

from ..text.prosody import derive_rate
from .models import DELIVERIES, EMOTIONS, clamp01
from .pronounce import match_pronunciations

KANA = re.compile(r"[\u3040-\u30ff]")
CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")

DELIVERY_ZH = {"shout": "喊叫", "whisper": "压低声音", "sneer": "带讥讽", "normal": "正常语气"}
DEFAULT_INTENSITY = 0.5


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


def resolve_emotion(row: dict, kind: str = "dialogue") -> dict:
    """旁白不带情绪；人物话术用模型给的主/副情绪，缺了按平静 + default 记录。"""
    if kind == "narration":
        return {"dominant": "平静", "intensity": 0.0, "source": "none"}
    dominant = row.get("emotion")
    if dominant not in EMOTIONS:
        return {"dominant": "平静", "intensity": DEFAULT_INTENSITY, "source": "default"}
    intensity = DEFAULT_INTENSITY if row.get("intensity") is None else clamp01(row.get("intensity"))
    mix = [{"name": dominant, "weight": round(intensity, 3)}]
    secondary = row.get("secondary")
    if secondary in EMOTIONS and secondary != dominant:
        weight = clamp01(0.3 if row.get("secondary_weight") is None else row.get("secondary_weight"))
        if weight > 0:
            mix.append({"name": secondary, "weight": round(min(weight, 0.5), 3)})
    return {"dominant": dominant, "intensity": round(intensity, 3), "source": "line", "mix": mix}


def derive_emotion_text(emotion: dict, delivery: str) -> str:
    """文本描述情绪通道用的一句话指令（通道默认关闭，这里只留兜底描述）。"""
    return (
        f"{emotion['dominant']}，幅度{emotion['intensity']:.2f}，"
        f"{DELIVERY_ZH.get(delivery, DELIVERY_ZH['normal'])}"
    )


def derive_line(
    row: dict,
    *,
    chapter_index: int,
    scene_index: int,
    seq: int,
    sentence: str,
    speaker_id: str,
    speaker_name: str,
    kind: str = "dialogue",
    pronounce_table: dict[str, str] | None = None,
) -> dict:
    kind = kind if kind in ("narration", "dialogue") else "narration"
    emotion = resolve_emotion(row, kind)
    delivery = row.get("delivery") if row.get("delivery") in DELIVERIES else "normal"
    return {
        "id": line_id(chapter_index, scene_index, seq),
        "scene": scene_id(chapter_index, scene_index),
        "scene_index": scene_index,
        "seq": seq,
        "kind": kind,
        "speaker": speaker_id,
        "speaker_name": speaker_name,
        "addressee": None,
        "addressee_name": None,
        "text": sentence,
        "emotion": emotion,
        "emotion_text": None if kind == "narration" else derive_emotion_text(emotion, delivery),
        "delivery": delivery,
        "lang": derive_lang(sentence),
        "rate": derive_rate(delivery, emotion if kind == "dialogue" else None),
        "pronounce": match_pronunciations(sentence, pronounce_table or {}),
    }
