"""角色整合：把「同一个人被叫了好几个名字」交给大模型合并，代码只做校验与 id 分配。

大模型只输出「主名 + 别名」；出现章数、台词数、role_id 由本地按确定性规则生成，
保证同一份标注跑两次得到同样的角色表（id 稳定 → 音频缓存不被无谓作废）。
"""

import time

from ..llm.runner import LlmJsonError
from .models import MergeOutput, SpokenLine, is_narrator, is_unknown
from .prompts import MERGE_SYSTEM, merge_user
from .roles import NARRATOR_ID, names_from_payload, next_role_number, normalize

MERGE_PASS = "merge"
SAMPLE_LIMIT = 6
SAMPLE_CHARS = 40


def role_entries(extractions: list[tuple[int, list[SpokenLine]]]) -> list[dict]:
    """把逐章提取结果汇成「称呼 + 样本台词」清单（旁白/未知不算人物）。"""
    buckets: dict[str, dict] = {}
    for chapter_index, lines in extractions:
        for position, item in enumerate(lines):
            name = (item.role or "").strip()
            if not name or is_narrator(name) or is_unknown(name):
                continue
            bucket = buckets.get(name)
            if bucket is None:
                bucket = buckets[name] = {
                    "name": name,
                    "count": 0,
                    "chapters": set(),
                    "samples": [],
                    "first": (int(chapter_index), position),
                }
            bucket["count"] += 1
            bucket["chapters"].add(int(chapter_index))
            if len(bucket["samples"]) < SAMPLE_LIMIT and item.text:
                bucket["samples"].append(item.text[:SAMPLE_CHARS])
    entries = [{**bucket, "chapters": sorted(bucket["chapters"])} for bucket in buckets.values()]
    entries.sort(key=lambda entry: (-len(entry["chapters"]), entry["first"], entry["name"]))
    return entries


def _narrator_entry() -> dict:
    return {
        "id": NARRATOR_ID,
        "name": "旁白",
        "aliases": [],
        "chapters": [],
        "mentions": 0,
        "first": [10**6, 10**6],
        "is_narrator": True,
    }


def _groups_from_output(output: MergeOutput, names: list[str], known: list[str]) -> tuple[list[dict], list[str]]:
    """校验模型输出：只认清单里出现过的称呼，每个称呼最多归属一条记录。"""
    valid = {normalize(name): name for name in [*names, *known] if name}
    groups: list[dict] = []
    assigned: set[str] = set()
    for item in output.characters:
        canonical = valid.get(normalize(item.name))
        if not canonical or normalize(canonical) in assigned:
            continue
        aliases: list[str] = []
        for alias in item.aliases:
            key = normalize(alias)
            if key not in valid or key in assigned or key == normalize(canonical):
                continue
            aliases.append(valid[key])
        groups.append({"name": canonical, "aliases": aliases})
        assigned.add(normalize(canonical))
        assigned.update(normalize(alias) for alias in aliases)
    missing = [name for name in names if normalize(name) not in assigned]
    for name in missing:
        groups.append({"name": name, "aliases": []})
        assigned.add(normalize(name))
    return groups, missing


def _run_merge(runner, *, book_id: str, entries: list[dict], known: list[str]) -> tuple[list[dict], list[dict]]:
    names = [entry["name"] for entry in entries]
    try:
        output = runner.run(
            system=MERGE_SYSTEM,
            user=merge_user(entries, known or None),
            model_cls=MergeOutput,
            pass_name=MERGE_PASS,
            book_id=book_id,
        )
    except LlmJsonError as exc:
        return (
            [{"name": name, "aliases": []} for name in names],
            [
                {
                    "kind": "role_merge_failed",
                    "reason": f"角色整合失败：{exc}",
                    "fallback": "每个称呼各自成为一个角色（可稍后重跑分析）",
                    "detail": {"names": names},
                }
            ],
        )
    groups, missing = _groups_from_output(output, names, known)
    issues = []
    if missing:
        issues.append(
            {
                "kind": "role_merge_incomplete",
                "reason": f"{len(missing)} 个称呼没有出现在整合结果里，已各自成角",
                "fallback": "各自成为一个角色",
                "detail": {"names": missing},
            }
        )
    return groups, issues


def _apply_groups(characters: list[dict], groups: list[dict], entries: list[dict]) -> list[dict]:
    """把「主名 + 别名」落到角色表上：已有角色保留 id，新角色追加编号。"""
    characters = list(characters or [])
    if not any(character.get("is_narrator") for character in characters):
        characters.insert(0, _narrator_entry())
    by_key: dict[str, dict] = {}
    for character in characters:
        by_key[normalize(character.get("name"))] = character
        for alias in character.get("aliases") or []:
            by_key.setdefault(normalize(alias), character)
    entry_by_name = {entry["name"]: entry for entry in entries}
    counter = next_role_number(characters)
    for group in groups:
        keys = [group["name"], *group["aliases"]]
        target = next((by_key[normalize(key)] for key in keys if normalize(key) in by_key), None)
        if target is None:
            counter += 1
            target = {
                "id": f"role_{counter:04d}",
                "name": group["name"],
                "aliases": [],
                "chapters": [],
                "mentions": 0,
                "first": [10**6, 10**6],
                "is_narrator": False,
            }
            characters.append(target)
            by_key[normalize(group["name"])] = target
        chapters = set(target.get("chapters") or [])
        mentions = int(target.get("mentions") or 0)
        first = tuple(target.get("first") or (10**6, 10**6))
        for key in keys:
            normalized = normalize(key)
            if not normalized:
                continue
            if normalized not in by_key:
                if key != target["name"] and key not in (target.get("aliases") or []):
                    target.setdefault("aliases", []).append(key)
                by_key[normalized] = target
            entry = entry_by_name.get(key)
            if entry is not None:
                chapters.update(entry.get("chapters") or [])
                mentions += int(entry.get("count") or 0)
                first = min(first, tuple(entry.get("first") or (10**6, 10**6)))
        target["chapters"] = sorted(chapters)
        target["mentions"] = mentions
        target["first"] = list(first)
    narrator = [character for character in characters if character.get("is_narrator")]
    others = [character for character in characters if not character.get("is_narrator")]
    others.sort(
        key=lambda character: (
            -len(character.get("chapters") or []),
            tuple(character.get("first") or (10**6, 10**6)),
            character.get("name") or "",
        )
    )
    return [*narrator, *others]


def _payload(book_id: str, characters: list[dict]) -> dict:
    names: dict[str, str] = {}
    for character in characters:
        names[character["name"]] = character["id"]
        for alias in character.get("aliases") or []:
            names.setdefault(alias, character["id"])
    return {
        "book_id": book_id,
        "generated_at": int(time.time() * 1000),
        "characters": characters,
        "names": names,
    }


def merge_roles(runner, *, book_id: str, entries: list[dict], known: list[str] = ()) -> tuple[dict, list[dict]]:
    """全书整合：entries 是 role_entries 的产出，known 是重跑时旧角色表里的名字。"""
    entries = [entry for entry in entries if entry.get("name")]
    if not entries:
        return _payload(book_id, [_narrator_entry()]), []
    groups, issues = _run_merge(runner, book_id=book_id, entries=entries, known=list(known))
    characters = _apply_groups([], groups, entries)
    return _payload(book_id, characters), issues


def extend_characters(
    runner,
    *,
    book_id: str,
    payload: dict,
    spoken: list[SpokenLine] | None = None,
    extractions: list[tuple[int, list[SpokenLine]]] | None = None,
) -> tuple[dict, list[dict]]:
    """单章/多章重跑时用：把新冒出来的称呼并进已有角色表（是别名就并进老角色）。

    extractions 用于一次并入多章（[(chapter_index, lines), ...]），与 spoken 二选一：
    多章时章号/首次出场要按真实章号记，否则新角色的 chapters 会全记成第 0 章。
    runner=None 时不调大模型，只按名字本地补角色（边提取边展示用的临时表，
    全书整合完成后再由 lines 任务用最终角色表覆盖）。
    """
    source = extractions if extractions is not None else [(0, spoken or [])]
    existing = names_from_payload(payload)
    known = {normalize(name) for name in existing}
    entries = [entry for entry in role_entries(source) if normalize(entry["name"]) not in known]
    if not entries:
        # 没有新称呼也要给出可用角色表：全旁白章节 / 首次分析时 payload 可能是空的
        return (payload or _payload(book_id, [_narrator_entry()])), []
    if runner is None:
        groups, issues = [{"name": entry["name"], "aliases": []} for entry in entries], []
    else:
        groups, issues = _run_merge(runner, book_id=book_id, entries=entries, known=existing)
    characters = _apply_groups(list(payload.get("characters") or []), groups, entries)
    return _payload(book_id, characters), issues
