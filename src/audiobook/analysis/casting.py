"""选角登记：角色表 → voices/casting.json（角色 → 基础音色描述的登记表）。

换 Qwen3-TTS 之后，角色音色不再从音色库里挑：
  - 每个角色（旁白也算一个角色）在这里登记一条 voice_source=design；
  - 具体"声音长什么样"由 analysis/design.py 让大模型写成基础音色描述，
    逐句合成时和该句的表演描述拼起来交给 VoiceDesign（见 analysis/voices.py）；
  - 音色库仍然保留成一条备用通道：用户手工给某个角色绑库存音色时，
    这条记录变成 voice_source=library，合成走克隆。

这个模块只做登记与校验，不调大模型（描述生成在 voice_design 任务里）。
"""

import json
import re
import time
from dataclasses import dataclass

from .roles import NARRATOR_ID

RECOMMEND_PASS = "casting"
MAX_RECOMMENDATIONS = 3
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
    tags: tuple[str, ...] = ()
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
    root = settings.voices_dir
    if not root.exists():
        return []
    voices: list[VoiceProfile] = []
    for directory in sorted(root.iterdir()):
        meta_path = directory / "voice.json"
        if not directory.is_dir() or not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("disabled"):
            continue  # 停用的音色不喂给大模型，也永远不会被推荐
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
                tags=_split_tags(_pick(data, "tags", default=[])),
                description=str(_pick(data, "description")),
            )
        )
    return voices


def voice_catalog_text(voices: list[VoiceProfile]) -> str:
    """音色库的文本清单（手工选音色 / 老数据展示用）。"""
    lines = []
    for voice in voices:
        base = "/".join(part for part in (voice.gender, voice.age_group, voice.speech_rate) if part)
        tags = [
            *voice.voice_quality,
            *voice.personality,
            *voice.mood,
            *voice.genres,
            *voice.usage_type,
            *voice.tags,
        ]
        deduped = list(dict.fromkeys(tag for tag in tags if tag))
        intro = voice.description.strip().replace("\n", " ")
        if len(intro) > 60:
            intro = intro[:60] + "…"
        detail = "/".join(part for part in (base, ",".join(deduped), intro) if part)
        lines.append(f"{voice.id}｜{voice.name}({detail})")
    return "\n".join(lines)


def samples_by_role(
    lines_by_chapter: dict[int, list[dict]], *, per_role: int = 8, max_chars: int = 40
) -> dict[str, list[str]]:
    """每个角色的台词样本（按出场顺序，最多 per_role 条）。"""
    out: dict[str, list[str]] = {}
    for index in sorted(lines_by_chapter):
        for row in lines_by_chapter[index]:
            name = str(row.get("speaker_name") or row.get("speaker") or "").strip()
            text = str(row.get("text") or "").strip()
            if not name or not text:
                continue
            bucket = out.setdefault(name, [])
            if len(bucket) >= per_role:
                continue
            bucket.append(text[:max_chars])
    return out


def _is_narrator(character: dict) -> bool:
    return bool(character.get("is_narrator")) or character.get("id") == NARRATOR_ID


def _entry(character: dict, previous: dict | None = None) -> dict:
    """登记一条角色音色记录：默认走"设计音色"，手选过库存音色的保持不动。"""
    old = previous or {}
    name = character.get("name") or character["id"]
    entry = {
        "role_id": character["id"],
        "name": name,
        "aliases": list(character.get("aliases") or []),
        "is_narrator": _is_narrator(character),
        "voice_source": "design",
        "voice_id": character["id"],
        "voice_name": f"{name}（设计音色）",
        "source": "design",
        # 基础音色描述：由 voice_design 任务写、用户可改
        "description": str(old.get("description") or ""),
        "description_source": old.get("description_source"),
        "sample": str(old.get("sample") or ""),
        "recommendations": [],
        "overrides": old.get("overrides") or {},
    }
    if old.get("updated_at"):
        entry["updated_at"] = old["updated_at"]
    if old.get("source") == "manual" and old.get("voice_id"):
        # 用户手工绑过库存音色：尊重手选（合成走克隆通道）
        entry.update(
            {
                "voice_source": "library",
                "voice_id": old["voice_id"],
                "voice_name": old.get("voice_name") or old["voice_id"],
                "source": "manual",
                "recommendations": old.get("recommendations") or [],
            }
        )
    return entry


def _names_map(characters: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for character in characters.get("characters") or []:
        names[character["name"]] = character["id"]
        for alias in character.get("aliases") or []:
            names.setdefault(alias, character["id"])
    return names


def _payload(book_id: str, roles: dict, characters: dict, voices: list[VoiceProfile]) -> dict:
    narrator = roles.get(NARRATOR_ID) or {}
    return {
        "book_id": book_id,
        "generated_at": _now_ms(),
        "voice_library_size": len(voices),
        "narrator_voice": narrator.get("voice_id"),
        "roles": roles,
        "names": _names_map(characters),
    }


def register_roles(
    *, book_id: str, characters: dict, previous: dict | None = None, voices: list[VoiceProfile] | None = None
) -> dict:
    """把所有角色登记进 casting.json（新角色补条目，老条目保留描述与手选）。"""
    old_roles = {role_id: dict(role) for role_id, role in ((previous or {}).get("roles") or {}).items()}
    roles: dict[str, dict] = {}
    for character in characters.get("characters") or []:
        roles[character["id"]] = _entry(character, old_roles.get(character["id"]))
    # 角色表里删掉的人不留在选角表里（避免幽灵角色）
    return _payload(book_id, roles, characters, voices or [])


def build_casting(
    runner,  # noqa: ARG001 - 保留签名：登记本身不再调大模型
    *,
    book_id: str,
    characters: dict,
    samples: dict | None = None,  # noqa: ARG001 - 同上（样本由 voice_design 用）
    voices: list[VoiceProfile] | None = None,
    previous: dict | None = None,
    concurrency: int = 4,  # noqa: ARG001
    on_progress=None,  # noqa: ARG001
) -> tuple[dict, list[dict]]:
    return register_roles(book_id=book_id, characters=characters, previous=previous, voices=voices), []


def fill_casting_for_characters(
    runner=None,  # noqa: ARG001
    *,
    book_id: str,
    characters: dict,
    samples: dict | None = None,  # noqa: ARG001
    voices: list[VoiceProfile] | None = None,
    previous: dict | None = None,
    concurrency: int = 4,  # noqa: ARG001
    on_progress=None,  # noqa: ARG001
) -> tuple[dict, list[dict]]:
    """单章分析后的增量登记：老角色的描述与手选原样保留。"""
    return register_roles(book_id=book_id, characters=characters, previous=previous, voices=voices), []


def _now_ms() -> int:
    return int(time.time() * 1000)


def voice_for_speaker(casting: dict, speaker: str) -> str | None:
    """支持 role_id / 主名 / 别名。"""
    roles = casting.get("roles") or {}
    names = casting.get("names") or {}
    key = (speaker or "").strip()
    role_id = key if key in roles else names.get(key)
    if role_id and role_id in roles:
        return roles[role_id].get("voice_id")
    return None
