"""角色音色描述：台词 → 大模型写"这个角色该是什么声音" → 存进 casting.json。

这是选角的替代方案：不再从音色库里挑一个现成音色，而是给每个角色（旁白也算一个
角色）写一段基础音色描述，逐句合成时再和该句的表演描述拼起来交给 TTS。

产物只有文本（快、可编辑）；试听音频按需生成（见 api 的 preview 接口），
不占分析时间。
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import store
from ..llm.runner import LlmJsonError
from .models import VoiceDesignOutput
from .prompts import VOICE_DESIGN_SYSTEM, voice_design_user
from .roles import NARRATOR_ID, characters_index

logger = logging.getLogger(__name__)

VOICE_DESIGN_PASS = "voice_design"
SAMPLE_LIMIT = 12
SAMPLE_CHARS = 60
# 大模型没给试音台词时的兜底（中性、涵盖常见音素，别用极端情绪句）
FALLBACK_SAMPLE = "你先坐下，慢慢说，我听着呢。"
NARRATOR_FALLBACK_SAMPLE = "夜色渐深，故事就从这里开始。"


def role_briefs(characters_payload: dict, lines_by_chapter: dict[int, list[dict]]) -> dict[str, dict]:
    """每个角色攒一份简报（台词样本 + 别名 + 出场章），旁白用叙述句当样本。"""
    index = characters_index(characters_payload)
    briefs: dict[str, dict] = {}
    for chapter in sorted(lines_by_chapter):
        for row in lines_by_chapter[chapter]:
            role_id = str(row.get("speaker") or "").strip()
            text = str(row.get("text") or "").strip()
            if not role_id or not text:
                continue
            kind = row.get("kind") or "dialogue"
            if kind == "dialogue" and role_id == NARRATOR_ID:
                continue  # 说话人写旁白但标成对白的脏数据：不算旁白的样本
            character = index.get(role_id) or {}
            is_narrator = role_id == NARRATOR_ID or bool(character.get("is_narrator"))
            if kind == "narration" and not is_narrator:
                continue  # 只收这个角色自己的话；旁白句不算角色的样本
            brief = briefs.setdefault(
                role_id,
                {
                    "role_id": role_id,
                    "name": "旁白" if is_narrator else (character.get("name") or row.get("speaker_name") or role_id),
                    "aliases": list(character.get("aliases") or []),
                    "samples": [],
                    "lines": 0,
                    "chapters": set(),
                    "is_narrator": is_narrator,
                },
            )
            brief["lines"] += 1
            brief["chapters"].add(int(chapter))
            if len(brief["samples"]) < SAMPLE_LIMIT:
                brief["samples"].append(text[:SAMPLE_CHARS])
    for brief in briefs.values():
        brief["chapters"] = sorted(brief["chapters"])
    return briefs


def describe_role(runner, *, book_id: str, brief: dict) -> tuple[dict, list[dict]]:
    """让大模型写这个角色的基础音色描述 + 试音台词。"""
    issues: list[dict] = []
    try:
        output = runner.run(
            system=VOICE_DESIGN_SYSTEM,
            user=voice_design_user(
                brief["name"],
                brief.get("aliases"),
                brief.get("samples") or [],
                brief.get("lines") or 0,
                is_narrator=bool(brief.get("is_narrator")),
            ),
            model_cls=VoiceDesignOutput,
            pass_name=VOICE_DESIGN_PASS,
            book_id=book_id,
        )
    except LlmJsonError as exc:
        return {}, [
            {
                "kind": "voice_design_failed",
                "reason": f"{brief['name']} 的音色描述生成失败：{exc}",
                "fallback": "沿用上一次的描述（没有就用兜底描述）",
                "detail": {"role_id": brief["role_id"]},
            }
        ]
    description = (output.description or "").strip()
    sample = (output.sample or "").strip()
    if not description:
        description = f"{brief['name']}的声音：自然、清晰、有辨识度"
        issues.append(
            {
                "kind": "voice_design_incomplete",
                "reason": f"{brief['name']} 的音色描述为空，已按角色名兜底",
                "fallback": description,
                "detail": {"role_id": brief["role_id"]},
            }
        )
    if not sample:
        fallback = NARRATOR_FALLBACK_SAMPLE if brief.get("is_narrator") else FALLBACK_SAMPLE
        # 优先用角色自己的第一句台词：试听时听到的就是他本人的话
        sample = (brief.get("samples") or [fallback])[0] or fallback
    return {"description": description, "sample": sample}, issues


def describe_many(runner, *, book_id: str, briefs: list[dict], concurrency: int = 4, on_progress=None):
    """并发跑大模型描述（纯文本，不占显存）。"""
    results: dict[str, tuple[dict, list[dict]]] = {}
    if not briefs:
        return results
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = {
            pool.submit(describe_role, runner, book_id=book_id, brief=brief): brief for brief in briefs
        }
        done = 0
        for future in as_completed(futures):
            brief = futures[future]
            try:
                results[brief["role_id"]] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单个角色失败不拖垮整批
                results[brief["role_id"]] = (
                    {},
                    [
                        {
                            "kind": "voice_design_failed",
                            "reason": f"{brief['name']} 的音色描述异常：{type(exc).__name__}: {exc}",
                            "fallback": "沿用上一次的描述（没有就用兜底描述）",
                            "detail": {"role_id": brief["role_id"]},
                        }
                    ],
                )
            done += 1
            if on_progress is not None:
                on_progress(done, len(briefs), brief["name"])
    return results


# ---------------------------------------------------------------- 落盘（casting.json）

def _casting(settings, book_id: str) -> dict:
    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    casting.setdefault("book_id", book_id)
    casting.setdefault("roles", {})
    return casting


def save_description(
    settings,
    book_id: str,
    role_id: str,
    *,
    description: str,
    sample: str = "",
    source: str = "llm",
    name: str | None = None,
) -> dict:
    """把基础音色描述写进 casting.json 的角色条目（没有条目就补一条）。"""
    casting = _casting(settings, book_id)
    roles = casting["roles"]
    entry = dict(roles.get(role_id) or {})
    entry.update(
        {
            "role_id": role_id,
            "name": name or entry.get("name") or role_id,
            "voice_source": "design",
            "voice_id": role_id,
            "description": (description or "").strip(),
            "description_source": source,
            "updated_at": int(time.time() * 1000),
        }
    )
    if sample:
        entry["sample"] = sample
    entry.setdefault("sample", FALLBACK_SAMPLE)
    entry.setdefault("source", "design")
    entry.setdefault("recommendations", [])
    entry.setdefault("overrides", {})
    roles[role_id] = entry
    casting["generated_at"] = int(time.time() * 1000)
    store.atomic_replace_json(store.casting_path(settings, book_id), casting)
    return entry


def pending_roles(
    characters_payload: dict,
    briefs: dict[str, dict],
    settings,
    book_id: str,
    roles: list[str] | None = None,
    force: bool = False,
) -> list[dict]:
    """哪些角色还没有基础音色描述（或强制重写）。roles 给了就只算这些角色。"""
    wanted = {str(role) for role in (roles or []) if role}
    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    known = casting.get("roles") or {}
    out: list[dict] = []
    for role_id, brief in briefs.items():
        if wanted and role_id not in wanted:
            continue
        entry = known.get(role_id) or {}
        if entry.get("voice_source") == "library" and entry.get("source") == "manual":
            continue  # 用户手工绑了库存音色：不动它
        if not force and entry.get("description") and entry.get("description_source") in ("llm", "manual"):
            continue
        out.append(brief)
    return out


def role_ids_with_lines(lines_by_chapter: dict[int, list[dict]]) -> dict[str, dict]:
    """只要"出现过哪些 role_id"时用的轻量版（不攒样本，别为了判断状态把整本书读一遍）。"""
    seen: dict[str, dict] = {}
    for chapter in sorted(lines_by_chapter):
        for row in lines_by_chapter[chapter]:
            role_id = str(row.get("speaker") or "").strip()
            if role_id and role_id not in seen:
                seen[role_id] = {"role_id": role_id, "name": role_id, "samples": [], "lines": 0}
    return seen
