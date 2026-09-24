import re

PUNCT = re.compile(r"[\s，。！？、；：\u201c\u201d\u2018\u2019（）《》…—·,.!?;:\u0022\u0027()\[\]<>-]+")


def _normalize(text: str) -> str:
    return PUNCT.sub("", str(text or ""))


def _normalize_title(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


BRACKETS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")


def _loose_title(text) -> str:
    """去掉"（求收藏）"这类后缀再比，避免只因为尾巴不同就整体错位。"""
    return BRACKETS.sub("", _normalize_title(text))


def _row(old: dict | None, new: dict | None, aligned_by: str) -> dict:
    old_title = str((old or {}).get("title") or "")
    new_title = str((new or {}).get("title") or "")
    old_chars = len((old or {}).get("content") or "")
    new_chars = len((new or {}).get("content") or "")
    old_index = int(old["index"]) if old is not None else None
    new_index = int(new["index"]) if new is not None else None
    return {
        "index": new_index if new_index is not None else old_index,
        "old_index": old_index,
        "new_index": new_index,
        "old_title": old_title,
        "new_title": new_title,
        "title_match": bool(old_title) and old_title == new_title,
        "aligned_by": aligned_by,
        "old_chars": old_chars,
        "new_chars": new_chars,
        "chars_delta": new_chars - old_chars,
    }


def compare_chapters(old_chapters: list[dict], new_chapters: list[dict]) -> dict:
    """逐章对照旧/新的分章结果：先按标题对齐，余下按位置对齐。"""
    old = list(old_chapters or [])
    new = list(new_chapters or [])
    used_old: set[int] = set()
    used_new: set[int] = set()
    rows: list[dict] = []
    for matcher, label in ((_normalize_title, "title"), (_loose_title, "title_loose")):
        for old_position, old_row in enumerate(old):
            if old_position in used_old:
                continue
            key = matcher(old_row.get("title"))
            if not key:
                continue
            for new_position, new_row in enumerate(new):
                if new_position in used_new:
                    continue
                if matcher(new_row.get("title")) == key:
                    rows.append(_row(old_row, new_row, label))
                    used_old.add(old_position)
                    used_new.add(new_position)
                    break
    rest_old = [position for position in range(len(old)) if position not in used_old]
    rest_new = [position for position in range(len(new)) if position not in used_new]
    for old_position, new_position in zip(rest_old, rest_new):
        rows.append(_row(old[old_position], new[new_position], "position"))
    for old_position in rest_old[len(rest_new):]:
        rows.append(_row(old[old_position], None, "old_only"))
    for new_position in rest_new[len(rest_old):]:
        rows.append(_row(None, new[new_position], "new_only"))
    rows.sort(key=lambda row: row["new_index"] if row["new_index"] is not None else 10**6 + (row["old_index"] or 0))
    return {
        "old_count": len(old),
        "new_count": len(new),
        "title_match_count": sum(1 for row in rows if row["aligned_by"] in ("title", "title_loose")),
        "chars_delta_total": sum(row["chars_delta"] for row in rows),
        "old_only": [row["old_title"] for row in rows if row["aligned_by"] == "old_only"],
        "new_only": [row["new_title"] for row in rows if row["aligned_by"] == "new_only"],
        "rows": rows,
    }


def compare_roles(old_roles: list[list[dict]], new_lines: dict[int, list[dict]]) -> dict:
    """把旧系统的逐句说话人当作基线，和新系统的标注比一致率。"""
    index: dict[str, str] = {}
    for chapter_index, rows in new_lines.items():
        for row in rows:
            key = _normalize(row.get("text"))
            if key:
                index.setdefault(key, str(row.get("speaker_name") or row.get("speaker") or ""))
    old_lines = matched = agree = 0
    legacy_narrator_reassigned = new_narrator_fallback = 0
    mismatches: list[dict] = []
    for chapter_rows in old_roles:
        for row in chapter_rows or []:
            old_lines += 1
            key = _normalize(row.get("text"))
            if not key or key not in index:
                continue
            matched += 1
            new_name = index[key]
            if new_name == str(row.get("role") or ""):
                agree += 1
                continue
            legacy = str(row.get("role") or "")
            if legacy in ("旁白", "narrator") and new_name not in ("旁白", "narrator"):
                legacy_narrator_reassigned += 1
            elif new_name in ("旁白", "narrator") and legacy not in ("旁白", "narrator"):
                new_narrator_fallback += 1
            if len(mismatches) < 20:
                mismatches.append({"text": row.get("text"), "legacy": legacy, "new": new_name})
    return {
        "old_lines": old_lines,
        "matched": matched,
        "agree": agree,
        "agreement_rate": round(agree / matched, 4) if matched else None,
        "mismatch_total": matched - agree,
        "legacy_narrator_reassigned": legacy_narrator_reassigned,
        "new_narrator_fallback": new_narrator_fallback,
        "mismatches": mismatches,
    }
