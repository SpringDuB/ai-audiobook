import re
import time
from dataclasses import dataclass

from .. import store

AGE_ORDER = ("儿童", "少年", "青年", "中年", "老年")
FAST_RATE = ("快", "轻快", "跳跃")
SLOW_RATE = ("慢", "拖沓")
TAG_SPLIT = re.compile(r"[/、,，\s]+")


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    gender: str = "未知"
    age_group: str = "未知"
    personality: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    mood: tuple[str, ...] = ()
    speech_rate: str = ""
    voice_quality: tuple[str, ...] = ()
    language_style: tuple[str, ...] = ()
    usage_type: tuple[str, ...] = ()
    description: str = ""


def _split_tags(values) -> tuple[str, ...]:
    out: list[str] = []
    if isinstance(values, str):
        values = [values]
    for value in values or ():
        for piece in TAG_SPLIT.split(str(value)):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return tuple(out)


def _pick(data: dict, *keys, default=""):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return default


def load_voice_library(settings) -> list[VoiceProfile]:
    root = store.voice_library_dir(settings)
    if not root.exists():
        return []
    voices: list[VoiceProfile] = []
    for directory in sorted(root.iterdir()):
        meta_path = directory / "voice.json"
        if not directory.is_dir() or not meta_path.exists():
            continue
        data = store.read_json(meta_path, default={}) or {}
        voices.append(
            VoiceProfile(
                id=str(_pick(data, "id", default=directory.name)),
                name=str(_pick(data, "name", default=directory.name)),
                gender=str(_pick(data, "gender", default="未知")),
                age_group=str(_pick(data, "age_group", "ageGroup", default="未知")),
                personality=_split_tags(_pick(data, "personality", default=[])),
                genres=_split_tags(_pick(data, "genres", default=[])),
                mood=_split_tags(_pick(data, "mood", default=[])),
                speech_rate=str(_pick(data, "speech_rate", "speechRate")),
                voice_quality=_split_tags(_pick(data, "voice_quality", "voiceQuality", default=[])),
                language_style=_split_tags(_pick(data, "language_style", "languageStyle", default=[])),
                usage_type=_split_tags(_pick(data, "usage_type", "usageType", default=[])),
                description=str(_pick(data, "description")),
            )
        )
    return voices


def _rate_bucket(rate: str) -> str:
    rate = (rate or "").strip()
    if rate in FAST_RATE:
        return "快"
    if rate in SLOW_RATE:
        return "慢"
    return "中"


def character_rate(character: dict) -> str:
    style = character.get("speaking_style") or ""
    if any(token in style for token in ("快", "急", "连珠")):
        return "快"
    if any(token in style for token in ("慢", "缓", "拖")):
        return "慢"
    return "中"


def _tag_hits(needles, haystack) -> list[str]:
    haystack = _split_tags(haystack) if isinstance(haystack, str) else tuple(haystack or ())
    hits: list[str] = []
    for needle in needles or ():
        needle = str(needle).strip()
        if len(needle) < 2:
            continue
        for tag in haystack:
            if len(tag) < 2:
                continue
            if needle in tag or tag in needle:
                hits.append(needle)
                break
    return hits


def score_voice(character, voice, *, is_narrator=False, book_genres=()) -> tuple[float, list[str], bool]:
    """角色 × 音色打分。返回（分数、理由、是否通过硬约束）。"""
    gender = character.get("gender") or "未知"
    if gender in ("男", "女") and voice.gender in ("男", "女") and voice.gender != gender:
        return 0.0, [f"性别不符：角色{gender} vs 音色{voice.gender}"], False
    score = 0.0
    reasons = ["性别不限" if gender == "未知" or voice.gender == "中性" else "性别匹配"]
    age = character.get("age_group") or "未知"
    if age in AGE_ORDER and voice.age_group in AGE_ORDER:
        distance = abs(AGE_ORDER.index(age) - AGE_ORDER.index(voice.age_group))
        if distance == 0:
            score += 30
            reasons.append("年龄段一致 +30")
        elif distance == 1:
            score += 12
            reasons.append("年龄段相邻 +12")
    if _rate_bucket(voice.speech_rate) == character_rate(character):
        score += 20
        reasons.append("语速一致 +20")
    personality = _split_tags(character.get("personality"))
    for needles, haystack, weight, cap, label in (
        (personality, voice.personality, 8, 24, "性格匹配"),
        (personality, voice.mood, 6, 18, "基调匹配"),
        (personality, voice.voice_quality, 5, 15, "音色匹配"),
        (_split_tags(book_genres), voice.genres, 5, 10, "题材先验"),
    ):
        hits = _tag_hits(needles, haystack)
        if hits:
            gained = min(weight * len(hits), cap)
            score += gained
            reasons.append(f"{label} +{gained}")
    description_hits = [tag for tag in personality if tag and tag in voice.description]
    if description_hits:
        gained = min(3 * len(description_hits), 9)
        score += gained
        reasons.append(f"描述命中 +{gained}")
    if is_narrator:
        if any(("旁白" in tag or "叙述" in tag or "播报" in tag) for tag in voice.usage_type):
            score += 12
            reasons.append("旁白叙述用途 +12")
    elif any("对话" in tag for tag in voice.usage_type):
        score += 6
        reasons.append("角色对话用途 +6")
    return round(score, 3), reasons, True


def build_casting(
    characters_payload: dict, voices: list[VoiceProfile], *, book_id="", book_genres=()
) -> tuple[dict, list[dict]]:
    """旁白先选并独占音色；其余角色按戏份挑最高分；池不够时复用并记异常。"""
    characters = characters_payload.get("characters", [])
    narrator = next((character for character in characters if character["is_narrator"]), None)
    others = sorted(
        (character for character in characters if not character["is_narrator"]),
        key=lambda character: (-len(character["chapters"]), character["name"]),
    )
    ordered = ([narrator] if narrator else []) + others
    names: dict[str, str] = {}
    for character in characters:
        names[character["name"]] = character["id"]
        for alias in character["aliases"]:
            names[alias] = character["id"]

    issues: list[dict] = []
    roles: dict[str, dict] = {}
    if not voices:
        for character in ordered:
            roles[character["id"]] = {
                "role_id": character["id"],
                "name": character["name"],
                "voice_id": "default",
                "voice_name": "未配置音色库",
                "score": 0.0,
                "reasons": ["音色库为空"],
                "overrides": {},
            }
        issues.append(
            {
                "kind": "voice_library_empty",
                "reason": "音色库为空，所有角色回落到 default（请先迁移音色库）",
                "fallback": "default",
                "detail": {"roles": len(ordered)},
            }
        )
        return {
            "book_id": book_id,
            "generated_at": int(time.time() * 1000),
            "voice_library_size": 0,
            "narrator_voice": "default",
            "roles": roles,
            "names": names,
        }, issues

    used: dict[str, str] = {}
    for character in ordered:
        is_narrator = bool(character["is_narrator"])
        scored = [
            (*score_voice(character, voice, is_narrator=is_narrator, book_genres=book_genres), voice)
            for voice in voices
        ]
        qualified = [item for item in scored if item[2]]
        relaxed = False
        if not qualified:
            qualified = scored
            relaxed = True
        qualified.sort(key=lambda item: (-item[0], item[3].id))
        fresh = [item for item in qualified if item[3].id not in used]
        reused = not fresh
        score, reasons, _, voice = fresh[0] if fresh else qualified[0]
        if relaxed:
            issues.append(
                {
                    "kind": "casting_no_match",
                    "reason": f"角色 {character['name']} 无合格音色，已放宽性别约束",
                    "fallback": voice.id,
                    "detail": {"role_id": character["id"]},
                }
            )
        if reused and not relaxed:
            issues.append(
                {
                    "kind": "casting_voice_reused",
                    "reason": f"音色 {voice.id} 被多个角色复用",
                    "fallback": voice.id,
                    "detail": {"role_id": character["id"], "shared_with": used.get(voice.id)},
                }
            )
        used.setdefault(voice.id, character["id"])
        roles[character["id"]] = {
            "role_id": character["id"],
            "name": character["name"],
            "voice_id": voice.id,
            "voice_name": voice.name,
            "score": score,
            "reasons": reasons,
            "overrides": {},
        }

    return {
        "book_id": book_id,
        "generated_at": int(time.time() * 1000),
        "voice_library_size": len(voices),
        "narrator_voice": roles.get(narrator["id"], {}).get("voice_id") if narrator else None,
        "roles": roles,
        "names": names,
    }, issues


def voice_for_speaker(casting: dict, speaker: str) -> str | None:
    """支持 role_id / 主名 / 别名，兼容 M0 的扁平格式 {名字: {voice_id}}。"""
    roles = casting.get("roles") or {}
    names = casting.get("names") or {}
    key = (speaker or "").strip()
    role_id = key if key in roles else names.get(key)
    if role_id and role_id in roles:
        return roles[role_id]["voice_id"]
    entry = casting.get(key)
    if isinstance(entry, dict):
        return entry.get("voice_id")
    return None
