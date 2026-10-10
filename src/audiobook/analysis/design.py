"""角色音色描述：台词 →（选角表）→ 大模型写"这个角色该是什么声音" → 存进 casting.json。

这是选角的替代方案：不再从音色库里挑一个现成音色，而是给每个角色（旁白也算一个
角色）写一段基础音色描述，逐句合成时再和该句的表演描述拼起来交给 TTS。

先跑一次"选角表"（book 级）：给所有角色定音色原型，强制两两之间拉开音区/质地，
否则每个角色各写各的，多个同类角色会撞成同一个声音；选角表失败不影响主流程。

产物只有文本（快、可编辑）；试听音频按需生成（见 api 的 preview 接口），
不占分析时间。
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import store
from ..llm.runner import LlmJsonError
from .models import CastSheetOutput, NARRATOR_NAMES, VoiceDesignOutput
from .prompts import CAST_SHEET_SYSTEM, VOICE_DESIGN_SYSTEM, cast_sheet_user, voice_design_user
from .roles import NARRATOR_ID, characters_index

logger = logging.getLogger(__name__)

VOICE_DESIGN_PASS = "voice_design"
CAST_SHEET_PASS = "cast_sheet"
SAMPLE_LIMIT = 12
SAMPLE_CHARS = 60
CLUE_LIMIT = 3
CLUE_CHARS = 60
# 选角表默认值（可在 .env 用 AB_CAST_SHEET_* 覆盖）：
# 一本书可能有 600+ 角色，一次全交给模型既不现实、效果也差 —— 只协调有戏份的角色，
# 并且分批做，后一批看得到前面已占用的原型。
CAST_SHEET_BATCH = 16
CAST_SHEET_MAX_ROLES = 120
CAST_SHEET_MIN_LINES = 4
# 出场少的角色不参与选角表，但要知道"主角群已经占了的音色"，别撞主角
AVOID_TOP = 10
AVOID_DESC_CHARS = 40
# 大模型没给试音台词时的兜底（中性、涵盖常见音素，别用极端情绪句）
FALLBACK_SAMPLE = "你先坐下，慢慢说，我听着呢。"
NARRATOR_FALLBACK_SAMPLE = "夜色渐深，故事就从这里开始。"

# 纯语气词/应答句：谁说出来都长一个样，进样本池只会把描述带偏
NOISE_CHARS = set("嗯啊哦噢喔哈嘿嗨咦呃呀哎唉喂嘛呵唔哟")
_PUNCTUATION = "，。！？、…～,.!?~ \t\u3000“”\"'‘’：；:;（）()"


def _plain(text: str) -> str:
    return "".join(ch for ch in text if ch not in _PUNCTUATION)


def _is_noise(text: str) -> bool:
    body = _plain(text)
    return len(body) <= 1 or all(ch in NOISE_CHARS for ch in body)


def _sample_score(text: str) -> int:
    """挑样本用的信息量打分：适中长度、带人称的句子优先，语气词垫底。"""
    score = min(len(text), 24)
    if any(mark in text for mark in ("我", "你", "您")):
        score += 6
    if len(text) <= 5:
        score -= 8
    if _is_noise(text):
        score -= 60
    return score


def _clip_sentence(text: str, limit: int) -> str:
    """按句读截断，别把样本切在半个词上（大模型可能原样拿去当试音台词）。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    best = -1
    for mark in ("。", "！", "？", "…", "；", "，", "：", "”"):
        best = max(best, head.rfind(mark))
    return head[: best + 1] if best >= limit // 2 else head


def _round_robin(buckets: dict[int, list[tuple[int, int, str]]], limit: int) -> list[str]:
    """逐章轮转取样本：先保证每个出现过的章节都出一句，再轮到第二句。

    旧实现取"全书前 12 句"，而角色第一次出场往往是"什么？""你是谁？"这类短句，
    描述只能靠猜。跨章抽样拿到的样本更能代表这个角色。
    """
    chapters = []
    for chapter in sorted(buckets):
        rows = sorted(buckets[chapter], key=lambda item: (-item[0], item[1]))
        chapters.append([text for _, _, text in rows])
    picked: list[str] = []
    seen: set[str] = set()
    cursor = 0
    while len(picked) < limit:
        moved = False
        for texts in chapters:
            if cursor >= len(texts):
                continue
            text = texts[cursor]
            moved = True
            if text not in seen:
                seen.add(text)
                picked.append(text)
                if len(picked) >= limit:
                    break
        if not moved:
            break
        cursor += 1
    return picked


def _narration_clues(brief: dict, narration: dict[int, list[str]]) -> list[str]:
    """旁白里提到这个角色的句子 —— 提示词里说的"身份线索"真正的来源。"""
    names = [brief["name"], *(brief.get("aliases") or [])]
    names = [name for name in dict.fromkeys(names) if name and name not in NARRATOR_NAMES]
    if not names:
        return []
    clues: list[str] = []
    for chapter in brief.get("chapters") or []:
        if len(clues) >= CLUE_LIMIT:
            break
        for text in narration.get(int(chapter)) or []:
            if len(text) < 8 or _is_noise(text):
                continue
            if any(name in text for name in names):
                clues.append(_clip_sentence(text, CLUE_CHARS))
                break  # 每章最多给一条：线索铺到不同章节
    return clues


def role_briefs(characters_payload: dict, lines_by_chapter: dict[int, list[dict]]) -> dict[str, dict]:
    """每个角色攒一份简报：跨章抽样的台词样本 + 别名 + 出场章 + 旁白身份线索。"""
    index = characters_index(characters_payload)
    briefs: dict[str, dict] = {}
    buckets: dict[str, dict[int, list[tuple[int, int, str]]]] = {}
    noise: dict[str, dict[int, list[tuple[int, int, str]]]] = {}
    narration: dict[int, list[str]] = {}
    for chapter in sorted(lines_by_chapter):
        for order, row in enumerate(lines_by_chapter[chapter]):
            role_id = str(row.get("speaker") or "").strip()
            text = str(row.get("text") or "").strip()
            if not role_id or not text:
                continue
            kind = row.get("kind") or "dialogue"
            character = index.get(role_id) or {}
            is_narrator = role_id == NARRATOR_ID or bool(character.get("is_narrator"))
            if kind == "narration":
                # 旁白句不是人物的台词，但它是"这个角色是谁"的线索来源
                narration.setdefault(int(chapter), []).append(text)
                if not is_narrator:
                    continue
            elif role_id == NARRATOR_ID:
                continue  # 说话人写旁白却标成对白的脏数据：不算旁白的样本
            brief = briefs.setdefault(
                role_id,
                {
                    "role_id": role_id,
                    "name": "旁白" if is_narrator else (character.get("name") or row.get("speaker_name") or role_id),
                    "aliases": list(character.get("aliases") or []),
                    "samples": [],
                    "clues": [],
                    "lines": 0,
                    "chapters": set(),
                    "is_narrator": is_narrator,
                },
            )
            brief["lines"] += 1
            brief["chapters"].add(int(chapter))
            # 纯语气词（"嗯。""哈哈。"）没有音色信息，只在"这个角色没有别的台词"时才用
            target = noise if _is_noise(text) else buckets
            target.setdefault(role_id, {}).setdefault(int(chapter), []).append(
                (_sample_score(text), order, _clip_sentence(text, SAMPLE_CHARS))
            )
    for brief in briefs.values():
        brief["chapters"] = sorted(brief["chapters"])
        role_id = brief["role_id"]
        brief["samples"] = _round_robin(buckets.get(role_id) or {}, SAMPLE_LIMIT) or _round_robin(
            noise.get(role_id) or {}, SAMPLE_LIMIT
        )
        if not brief["is_narrator"]:
            brief["clues"] = _narration_clues(brief, narration)
    return briefs


def cast_sheet_rows(briefs: list[dict], known: dict[str, dict] | None = None) -> list[dict]:
    """选角表的输入行：待定角色给台词样本，已经有描述的角色给"已定音色"。"""
    rows: list[dict] = []
    for brief in briefs:
        entry = (known or {}).get(brief["role_id"]) or {}
        description = str(entry.get("description") or "").strip()
        if description:
            note = "已定音色：" + description
        else:
            note = "台词：" + " | ".join((brief.get("samples") or [])[:4])
        rows.append({"name": brief["name"], "lines": brief.get("lines") or 0, "note": note})
    return rows


def build_cast_sheet(
    runner,
    *,
    book_id: str,
    briefs: list[dict],
    known: dict[str, dict] | None = None,
    batch_size: int = CAST_SHEET_BATCH,
    max_roles: int = CAST_SHEET_MAX_ROLES,
    min_lines: int = CAST_SHEET_MIN_LINES,
    on_progress=None,
    cancel_check=None,
) -> tuple[dict[str, str], list[dict]]:
    """分批给"有戏份"的角色定音色原型，保证听众分得清谁是谁。失败就退回逐个独立设计。

    为什么要分批：600+ 角色的书不可能一次全塞给模型（提示词爆炸、注意力必然稀释）。
    做法是只让台词数 ≥ min_lines、且按台词数排在前面（最多 max_roles 个）的角色参与，
    每批 batch_size 个；每批都能看到前面几批已经定下的原型（增量避让），
    所以跨批之间也不会撞。出场极少的龙套不参与，但拿得到主角群的已占原型当避让提示。

    增量：已经有音色（原型或基础描述）的角色不再重新定，只留在"已占用音色"里当参照；
    所以补新角色/重跑时只给全新的角色分批，不会把整本书的选角表再烧一遍。
    """
    known = known or {}
    participants = cast_sheet_participants(briefs, min_lines=min_lines, max_roles=max_roles)
    pending = [brief for brief in participants if not voice_hint(known.get(brief["role_id"]))]
    if not pending:
        return {}, []
    pending_ids = {brief["role_id"] for brief in pending}
    others = [brief for brief in participants if brief["role_id"] not in pending_ids]
    if len(pending) < 2 and not others:
        return {}, []  # 单角色书：没有可协调的对象，保持原行为
    size = max(2, int(batch_size))
    batches = [pending[index : index + size] for index in range(0, len(pending), size)]
    archetypes: dict[str, str] = {}
    issues: list[dict] = []
    for index, batch in enumerate(batches, start=1):
        if cancel_check is not None:
            cancel_check()
        names = [brief["name"] for brief in batch]
        # 这一批自己的角色已经在"角色清单"里了，别再重复出现在"已占用"里
        avoid = [row for row in _avoid_rows(participants, archetypes, known) if row["name"] not in names]
        try:
            output = runner.run(
                system=CAST_SHEET_SYSTEM,
                user=cast_sheet_user(cast_sheet_rows(batch, known), avoid=avoid),
                model_cls=CastSheetOutput,
                pass_name=CAST_SHEET_PASS,
                book_id=book_id,
                cancel_check=cancel_check,
            )
        except LlmJsonError as exc:
            issues.append(
                {
                    "kind": "cast_sheet_failed",
                    "reason": f"选角表第 {index}/{len(batches)} 批失败（{'、'.join(names[:6])}），"
                    f"这些角色改为按台词独立设计（可能撞车）：{exc}",
                    "fallback": "逐角色独立描述",
                    "detail": {"batch": index, "roles": names},
                }
            )
            if on_progress is not None:
                on_progress(index, len(batches), f"第 {index} 批失败，继续下一批")
            continue
        for item in output.characters:
            name = item.name.strip()
            archetype = item.archetype.strip()
            if name in names and archetype and name not in archetypes:
                archetypes[name] = archetype
        missing = [name for name in names if name not in archetypes]
        if missing:
            issues.append(
                {
                    "kind": "cast_sheet_failed",
                    "reason": "选角表漏了这些角色：" + "、".join(missing[:8]),
                    "fallback": "漏掉的角色不带原型，按台词独立设计",
                    "detail": {"batch": index, "missing": missing[:20]},
                }
            )
        if on_progress is not None:
            on_progress(index, len(batches), f"已定 {len(archetypes)} 个角色的原型")
    return archetypes, issues


def cast_sheet_participants(
    briefs: list[dict],
    *,
    min_lines: int = CAST_SHEET_MIN_LINES,
    max_roles: int = CAST_SHEET_MAX_ROLES,
) -> list[dict]:
    """谁参与跨角色音色协调：台词数 ≥ min_lines 的角色，按台词数从多到少取前 max_roles。

    旁白永远参与 —— 它是全书出现次数最多的声音，所有角色都得绕开它（哪怕某一版
    分章数据里旁白句数很少）。
    """
    ranked = sorted(
        briefs, key=lambda brief: (-int(brief.get("lines") or 0), str(brief.get("name") or ""))
    )
    picked = [brief for brief in ranked if brief.get("is_narrator")] + [
        brief
        for brief in ranked
        if not brief.get("is_narrator") and int(brief.get("lines") or 0) >= int(min_lines)
    ]
    return picked[: max(0, int(max_roles))]


def voice_hint(entry: dict | None) -> str:
    """一个角色当前已占用的音色提示：原型优先，其次取描述前缀。

    单角色重写（换一版音色）不重跑选角表，用它复用已有原型 / 回写新描述。
    """
    entry = entry or {}
    text = str(entry.get("archetype") or "").strip()
    if text:
        return text
    description = str(entry.get("description") or "").strip()
    return _clip_sentence(description, AVOID_DESC_CHARS) if description else ""


def _avoid_rows(briefs: list[dict], archetypes: dict[str, str], known: dict[str, dict]) -> list[dict]:
    """已经占用的音色：优先用本次刚定的原型，其次用角色已有的原型/描述前缀。"""
    rows: list[dict] = []
    for brief in briefs:
        name = brief["name"]
        text = str(archetypes.get(name) or "").strip()
        if not text:
            text = voice_hint(known.get(brief["role_id"]))
        if text:
            rows.append({"name": name, "archetype": text})
    return rows


def top_avoid_rows(archetypes: dict[str, str], briefs: list[dict], limit: int = AVOID_TOP) -> list[dict]:
    """出场少的角色拿到的避让提示：主角群前 limit 个已占原型（短、够用）。"""
    ranked = sorted(briefs, key=lambda brief: (-int(brief.get("lines") or 0), str(brief.get("name") or "")))
    rows: list[dict] = []
    for brief in ranked:
        text = str(archetypes.get(brief["name"]) or "").strip()
        if text:
            rows.append({"name": brief["name"], "archetype": text})
        if len(rows) >= limit:
            break
    return rows


def describe_role(
    runner,
    *,
    book_id: str,
    brief: dict,
    archetype: str = "",
    previous: str = "",
    directive: str = "",
    avoid: list[dict] | None = None,
    cancel_check=None,
) -> tuple[dict, list[dict]]:
    """让大模型写这个角色的基础音色描述 + 试音台词。

    archetype：选角表给的原型（和别的角色拉开距离）；previous：上一版描述，
    只在"微调"时传 —— 用户听过的音色不该因为一次重写就换掉。
    directive：用户自己写的换音色要求（"换一版音色"时填的那段话），硬性生效。
    avoid：别的角色已经占用的音色（主角群），提示模型别撞。
    """
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
                clues=brief.get("clues") or [],
                archetype=archetype,
                previous=previous,
                directive=directive,
                avoid=avoid,
            ),
            model_cls=VoiceDesignOutput,
            pass_name=VOICE_DESIGN_PASS,
            book_id=book_id,
            cancel_check=cancel_check,
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


def describe_many(
    runner,
    *,
    book_id: str,
    briefs: list[dict],
    archetypes: dict[str, str] | None = None,
    previous: dict[str, str] | None = None,
    directives: dict[str, str] | None = None,
    avoid: list[dict] | None = None,
    concurrency: int = 4,
    on_progress=None,
    cancel_check=None,
):
    """并发跑大模型描述（纯文本，不占显存）。

    archetypes 按角色名索引（选角表的输出），previous / directives 按 role_id 索引
    （微调锚点 / 用户的换音色要求），avoid 是全书共用的"已被占用音色"避让清单。
    """
    results: dict[str, tuple[dict, list[dict]]] = {}
    if not briefs:
        return results
    archetypes = archetypes or {}
    previous = previous or {}
    directives = directives or {}
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = {
            pool.submit(
                describe_role,
                runner,
                book_id=book_id,
                brief=brief,
                archetype=archetypes.get(brief["name"], ""),
                previous=previous.get(brief["role_id"], ""),
                directive=directives.get(brief["role_id"], ""),
                avoid=avoid,
                cancel_check=cancel_check,
            ): brief
            for brief in briefs
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
    archetype: str | None = None,
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
    if archetype:
        # 选角表给的原型：重跑补新角色时要拿它当"已定音色"避开撞车
        entry["archetype"] = archetype
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
