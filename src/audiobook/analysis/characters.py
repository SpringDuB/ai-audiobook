import re

from .models import NARRATOR_NAMES, PassAOutput
from .prompts import PASS_A_SYSTEM, pass_a_user

SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")
NARRATOR_ID = "narrator"


def chunk_text(text: str, max_chars: int) -> list[str]:
    """按句子边界把正文切成不超过 max_chars 的分块，拼接后等于原文。"""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return [text] if text else []
    pieces = [piece for piece in SENTENCE_SPLIT.split(text) if piece]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > limit:  # 单句超长时硬切，保证不丢字
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if len(current) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


def extract_chapter(runner, *, settings, book_id: str, chapter_index: int, title: str, content: str) -> PassAOutput:
    chunks = chunk_text(content, settings.llm_chunk_chars) or [content]
    outputs = [
        runner.run(
            system=PASS_A_SYSTEM,
            user=pass_a_user(chapter_index, title, chunk),
            model_cls=PassAOutput,
            pass_name="A",
            book_id=book_id,
            chapter_index=chapter_index,
        )
        for chunk in chunks
    ]
    return merge_pass_a(outputs)


def merge_pass_a(outputs: list[PassAOutput]) -> PassAOutput:
    characters = [card for out in outputs for card in out.characters]
    relationships = [rel for out in outputs for rel in out.relationships]
    return PassAOutput(characters=characters, relationships=relationships)


def _norm(name: str) -> str:
    return (name or "").strip().replace(" ", "")


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item] = root
            item = self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def _vote(items, field: str, unknown=("", "未知")) -> str:
    counts: dict[str, int] = {}
    for _, card in items:
        value = getattr(card, field)
        if value in unknown:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return "未知"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _ordered_union(items, field: str, cap: int) -> list[str]:
    out: list[str] = []
    for _, card in items:
        for value in getattr(card, field):
            value = (value or "").strip()
            if value and value not in out:
                out.append(value)
            if len(out) >= cap:
                return out
    return out


def _first_non_empty(items, field: str) -> str:
    for _, card in items:
        value = (getattr(card, field) or "").strip()
        if value:
            return value
    return ""


def aggregate_characters(per_chapter: list[tuple[int, PassAOutput]]) -> dict:
    """把每章 Pass A 结果聚合成全书角色档案（含别名合并与有向关系矩阵）。"""
    uf = _UnionFind()
    records: list[tuple[int, object]] = []
    for chapter_index, out in per_chapter:
        for card in out.characters:
            name = _norm(card.name)
            if not name:
                continue
            uf.add(name)
            for alias in card.aliases:
                alias_norm = _norm(alias)
                if alias_norm:
                    uf.union(name, alias_norm)
            records.append((chapter_index, card))

    groups: dict[str, list[tuple[int, object]]] = {}
    for chapter_index, card in records:
        groups.setdefault(uf.find(_norm(card.name)), []).append((chapter_index, card))

    built: list[dict] = []
    for root, items in groups.items():
        names: set[str] = set()
        for _, card in items:
            names.add(_norm(card.name))
            names.update(_norm(alias) for alias in card.aliases if _norm(alias))

        def appearance(name: str) -> tuple[int, int]:
            chapters = {
                chapter_index
                for chapter_index, card in items
                if name in {_norm(card.name), *[_norm(alias) for alias in card.aliases]}
            }
            return len(chapters), (min(chapters) if chapters else 10 ** 6)

        ordered_names = sorted(names, key=lambda name: (-appearance(name)[0], appearance(name)[1], name))
        canonical = ordered_names[0]
        primary = next((card for _, card in items if _norm(card.name) == canonical), items[0][1])
        chapters = sorted({chapter_index for chapter_index, _ in items})
        built.append(
            {
                "name": canonical,
                "aliases": ordered_names[1:],
                "gender": _vote(items, "gender"),
                "age_group": _vote(items, "age_group"),
                "personality": _ordered_union(items, "personality", cap=8),
                "speaking_style": _first_non_empty(items, "speaking_style"),
                "base_emotion": primary.base_emotion,
                "base_intensity": round(primary.base_intensity, 3),
                "chapters": chapters,
                "mentions": len(items),
                "is_narrator": canonical in NARRATOR_NAMES,
            }
        )

    if not any(item["is_narrator"] for item in built):
        built.append(
            {
                "name": "旁白",
                "aliases": [],
                "gender": "未知",
                "age_group": "未知",
                "personality": [],
                "speaking_style": "平稳",
                "base_emotion": "平静",
                "base_intensity": 0.3,
                "chapters": [],
                "mentions": 0,
                "is_narrator": True,
            }
        )

    indexed = list(enumerate(built))
    narrator = next(item for _, item in indexed if item["is_narrator"])
    others = sorted(
        ((position, item) for position, item in indexed if not item["is_narrator"]),
        key=lambda pair: (
            -len(pair[1]["chapters"]),
            pair[1]["chapters"][0] if pair[1]["chapters"] else 10 ** 6,
            pair[0],  # 章内出现顺序：先出场的角色拿到更小的 role 号
        ),
    )
    characters: list[dict] = []
    name_to_id: dict[str, str] = {}
    counter = 0
    for item in [narrator, *[item for _, item in others]]:
        if item["is_narrator"]:
            role_id = NARRATOR_ID
        else:
            counter += 1
            role_id = f"role_{counter:04d}"
        entry = {"id": role_id, **item}
        characters.append(entry)
        name_to_id[_norm(entry["name"])] = role_id
        for alias in entry["aliases"]:
            name_to_id[_norm(alias)] = role_id

    merged: dict[tuple[str, str], dict] = {}
    dropped = 0
    for chapter_index, out in per_chapter:
        for rel in out.relationships:
            source = name_to_id.get(_norm(rel.source))
            target = name_to_id.get(_norm(rel.target))
            if not source or not target or source == target:
                dropped += 1
                continue
            agg = merged.setdefault(
                (source, target),
                {"from": source, "to": target, "sums": [0.0, 0.0, 0.0, 0.0], "n": 0, "note": "", "chapters": []},
            )
            for position, value in enumerate((rel.closeness, rel.hierarchy, rel.hostility, rel.intimacy)):
                agg["sums"][position] += value
            agg["n"] += 1
            if not agg["note"] and rel.note:
                agg["note"] = rel.note
            if chapter_index not in agg["chapters"]:
                agg["chapters"].append(chapter_index)

    relationships = [
        {
            "from": agg["from"],
            "to": agg["to"],
            "closeness": round(agg["sums"][0] / agg["n"], 3),
            "hierarchy": round(agg["sums"][1] / agg["n"], 3),
            "hostility": round(agg["sums"][2] / agg["n"], 3),
            "intimacy": round(agg["sums"][3] / agg["n"], 3),
            "note": agg["note"],
            "chapters": sorted(agg["chapters"]),
        }
        for _, agg in sorted(merged.items())
    ]

    return {
        "characters": characters,
        "relationships": relationships,
        "chapters": [
            {
                "index": chapter_index,
                "characters": [card.model_dump() for card in out.characters],
                "relationships": [rel.model_dump(by_alias=True) for rel in out.relationships],
            }
            for chapter_index, out in per_chapter
        ],
        "dropped_relationships": dropped,
    }


def characters_index(payload: dict) -> dict[str, dict]:
    return {character["id"]: character for character in payload.get("characters", [])}


def relationships_index(payload: dict) -> dict[tuple[str, str], dict]:
    return {(rel["from"], rel["to"]): rel for rel in payload.get("relationships", [])}


def resolve_speaker(payload: dict, name: str) -> str | None:
    target = _norm(name)
    if not target:
        return None
    for character in payload.get("characters", []):
        if target == _norm(character["name"]) or target in {_norm(alias) for alias in character["aliases"]}:
            return character["id"]
    return None


def role_name(payload: dict, role_id: str) -> str:
    for character in payload.get("characters", []):
        if character["id"] == role_id:
            return character["name"]
    return role_id
