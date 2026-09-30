"""选角：音色推荐由大模型给出（1–3 个），本地只做校验、兜底与手选保留。

推荐不能用规则打分：提示词把「角色 + 台词样本 + 音色库（id/名称/标签）」交给模型，
返回的 voiceId 必须落在音色库里；模型失败时按音色库顺序兜底并写异常清单。
用户手选过的角色（source=manual）在重跑选角时保持不动。
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..llm.runner import LlmJsonError
from .models import RecommendOutput
from .prompts import RECOMMEND_SYSTEM, recommend_user
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
    """提示词里的音色库描述：每行「id｜名称(性别/年龄/语速/音色/性格、题材、用途)」。"""
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
        # 上传音色往往只有一段介绍：截断后也喂给模型，否则模型只能靠名字猜
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


def recommend_for_character(
    runner, *, book_id: str, character: dict, samples: list[str], voices: list[VoiceProfile]
) -> tuple[list[dict], list[dict]]:
    catalog = voice_catalog_text(voices)
    try:
        output = runner.run(
            system=RECOMMEND_SYSTEM,
            user=recommend_user(character.get("name") or character["id"], samples, catalog),
            model_cls=RecommendOutput,
            pass_name=RECOMMEND_PASS,
            book_id=book_id,
        )
    except LlmJsonError as exc:
        return [], [
            {
                "kind": "voice_recommend_failed",
                "reason": f"{character.get('name') or character['id']} 的音色推荐失败：{exc}",
                "fallback": "按音色库顺序兜底",
                "detail": {"role_id": character["id"]},
            }
        ]
    known = {voice.id: voice for voice in voices}
    picked: list[dict] = []
    seen: set[str] = set()
    for item in output.recommendations:
        voice = known.get(item.voice_id)
        if voice is None or voice.id in seen:
            continue
        seen.add(voice.id)
        picked.append(
            {
                "voice_id": voice.id,
                "voice_name": voice.name,
                "confidence": item.confidence,
                "reason": item.reason,
            }
        )
        if len(picked) >= MAX_RECOMMENDATIONS:
            break
    if not picked:
        return [], [
            {
                "kind": "voice_recommend_invalid",
                "reason": f"{character.get('name') or character['id']} 的推荐里没有合法音色 id",
                "fallback": "按音色库顺序兜底",
                "detail": {"role_id": character["id"]},
            }
        ]
    return picked, []


def _fallback_voice(voices: list[VoiceProfile], used: set[str]) -> VoiceProfile:
    for voice in voices:
        if voice.id not in used:
            return voice
    return voices[0]


def _names_map(characters: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for character in characters.get("characters") or []:
        names[character["name"]] = character["id"]
        for alias in character.get("aliases") or []:
            names.setdefault(alias, character["id"])
    return names


def _casting_payload(book_id: str, roles: dict, characters: dict, voices: list[VoiceProfile]) -> dict:
    narrator = roles.get(NARRATOR_ID)
    return {
        "book_id": book_id,
        "generated_at": _now_ms(),
        "voice_library_size": len(voices),
        "narrator_voice": narrator["voice_id"] if narrator else None,
        "roles": roles,
        "names": _names_map(characters),
    }


def _role_entry(character: dict, *, voice_id: str, voice_name: str, source: str,
                recommendations: list[dict], overrides: dict | None = None) -> dict:
    return {
        "role_id": character["id"],
        "name": character["name"],
        "aliases": list(character.get("aliases") or []),
        "voice_id": voice_id,
        "voice_name": voice_name,
        "source": source,
        "recommendations": recommendations,
        "overrides": overrides or {},
    }


def fill_casting_for_characters(
    runner,
    *,
    book_id: str,
    characters: dict,
    samples: dict[str, list[str]],
    voices: list[VoiceProfile],
    previous: dict | None = None,
    concurrency: int = 4,
    on_progress=None,
) -> tuple[dict, list[dict]]:
    """给"还没有推荐"的角色补音色推荐（单章分析后的增量选角）。

    与 build_casting 的区别：只对缺推荐的角色调大模型，已经选过/已有推荐的
    角色原样保留（含用户手选），所以点一次「分析本章台词」只会为本章新冒出来的
    角色补 1–3 个推荐，不会把整本书的选角重算一遍。
    """
    definitions = list(characters.get("characters") or [])
    previous_payload = previous or {}
    roles = {role_id: dict(role) for role_id, role in (previous_payload.get("roles") or {}).items()}

    pending: list[dict] = []
    for character in definitions:
        existing = roles.get(character["id"]) or {}
        if existing.get("recommendations"):
            continue  # 已经有推荐（或手选）的角色不动
        pending.append(character)

    if not voices:
        for character in pending:
            roles[character["id"]] = _role_entry(
                character,
                voice_id="default",
                voice_name="未配置音色库",
                source="default",
                recommendations=[],
            )
        issues = (
            [
                {
                    "kind": "voice_library_empty",
                    "reason": "音色库为空，这些角色回落到 default（请先迁移音色库）",
                    "fallback": "default",
                    "detail": {"roles": len(pending)},
                }
            ]
            if pending
            else []
        )
        return _casting_payload(book_id, roles, characters, voices), issues

    results: dict[str, tuple[list[dict], list[dict]]] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
            futures = {
                pool.submit(
                    recommend_for_character,
                    runner,
                    book_id=book_id,
                    character=character,
                    samples=samples.get(character["name"]) or samples.get(character["id"]) or [],
                    voices=voices,
                ): character
                for character in pending
            }
            done = 0
            for future in as_completed(futures):
                character = futures[future]
                try:
                    results[character["id"]] = future.result()
                except Exception as exc:  # noqa: BLE001 - 单个角色失败不拖垮本章
                    results[character["id"]] = (
                        [],
                        [
                            {
                                "kind": "voice_recommend_failed",
                                "reason": f"{character['name']} 的音色推荐异常：{type(exc).__name__}: {exc}",
                                "fallback": "按音色库顺序兜底",
                                "detail": {"role_id": character["id"]},
                            }
                        ],
                    )
                done += 1
                if on_progress is not None:
                    on_progress(done, len(pending), character["name"])

    issues: list[dict] = []
    used = {role.get("voice_id") for role in roles.values() if role.get("voice_id")}
    for character in pending:
        recs, rec_issues = results.get(character["id"], ([], []))
        issues.extend(rec_issues)
        old = roles.get(character["id"]) or {}
        manual = old.get("source") == "manual" and old.get("voice_id")
        if manual:
            voice_id, source = old["voice_id"], "manual"
        elif recs:
            voice_id, source = recs[0]["voice_id"], "llm"
        else:
            voice_id, source = _fallback_voice(voices, used).id, "fallback"
        used.add(voice_id)
        voice = next((item for item in voices if item.id == voice_id), None)
        roles[character["id"]] = _role_entry(
            character,
            voice_id=voice_id,
            voice_name=voice.name if voice else voice_id,
            source=source,
            recommendations=recs or old.get("recommendations") or [],
            overrides=old.get("overrides") or {},
        )
    return _casting_payload(book_id, roles, characters, voices), issues


def build_casting(
    runner,
    *,
    book_id: str,
    characters: dict,
    samples: dict[str, list[str]],
    voices: list[VoiceProfile],
    previous: dict | None = None,
    concurrency: int = 4,
    on_progress=None,
) -> tuple[dict, list[dict]]:
    definitions = list(characters.get("characters") or [])
    previous_roles = (previous or {}).get("roles") or {}
    if not voices:
        roles = {
            character["id"]: _role_entry(
                character,
                voice_id="default",
                voice_name="未配置音色库",
                source="default",
                recommendations=[],
            )
            for character in definitions
        }
        return (
            {
                **_casting_payload(book_id, roles, characters, voices),
                "narrator_voice": "default",
            },
            [
                {
                    "kind": "voice_library_empty",
                    "reason": "音色库为空，所有角色回落到 default（请先迁移音色库）",
                    "fallback": "default",
                    "detail": {"roles": len(definitions)},
                }
            ],
        )

    results: dict[str, tuple[list[dict], list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = {
            pool.submit(
                recommend_for_character,
                runner,
                book_id=book_id,
                character=character,
                samples=samples.get(character["name"]) or samples.get(character["id"]) or [],
                voices=voices,
            ): character
            for character in definitions
        }
        done = 0
        for future in as_completed(futures):
            character = futures[future]
            try:
                results[character["id"]] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单个角色失败不拖垮选角
                results[character["id"]] = (
                    [],
                    [
                        {
                            "kind": "voice_recommend_failed",
                            "reason": f"{character['name']} 的音色推荐异常：{type(exc).__name__}: {exc}",
                            "fallback": "按音色库顺序兜底",
                            "detail": {"role_id": character["id"]},
                        }
                    ],
                )
            done += 1
            if on_progress is not None:
                on_progress(done, len(definitions), character["name"])

    roles: dict[str, dict] = {}
    issues: list[dict] = []
    used: set[str] = set()
    for character in definitions:
        recs, rec_issues = results.get(character["id"], ([], []))
        issues.extend(rec_issues)
        manual = previous_roles.get(character["id"]) or {}
        manual_voice = manual.get("voice_id") if manual.get("source") == "manual" else None
        if manual_voice and any(voice.id == manual_voice for voice in voices):
            voice_id, source = manual_voice, "manual"
        elif recs:
            voice_id, source = recs[0]["voice_id"], "llm"
        else:
            voice_id, source = _fallback_voice(voices, used).id, "fallback"
        used.add(voice_id)
        voice = next((item for item in voices if item.id == voice_id), None)
        roles[character["id"]] = _role_entry(
            character,
            voice_id=voice_id,
            voice_name=voice.name if voice else voice_id,
            source=source,
            recommendations=recs,
            overrides=manual.get("overrides") or {},
        )
    return _casting_payload(book_id, roles, characters, voices), issues


def _now_ms() -> int:
    return int(time.time() * 1000)


def voice_for_speaker(casting: dict, speaker: str) -> str | None:
    """支持 role_id / 主名 / 别名。"""
    roles = casting.get("roles") or {}
    names = casting.get("names") or {}
    key = (speaker or "").strip()
    role_id = key if key in roles else names.get(key)
    if role_id and role_id in roles:
        return roles[role_id]["voice_id"]
    return None
