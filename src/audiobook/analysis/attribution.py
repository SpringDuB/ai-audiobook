"""从归属句里读出"这话是谁说的"，给整章分析当提示、也给漏判兜底。

中文小说里说话人基本靠归属句点名：`王胖子一听，吃惊道。`、`张卫东歉意的笑道：`。
引语切出来之后这些线索就在相邻的旁白单元里，规则能读得很准——
比让模型对着「台词＋描述」混在一起的句子猜靠谱得多。
"""

from ..text.dialogue import ATTRIBUTION_AFTER, SPEECH_CUE


def names_from_payload(characters_payload: dict) -> list[str]:
    """角色表里的本名 + 别名（本名在前）。"""
    names: list[str] = []
    for character in characters_payload.get("characters") or []:
        primary = (character.get("name") or "").strip()
        if primary and primary not in names:
            names.append(primary)
        for alias in character.get("aliases") or []:
            alias = (alias or "").strip()
            if alias and alias not in names:
                names.append(alias)
    return names


def _unique_name(text: str, names: list[str]) -> str | None:
    """文本里只提到一个角色时返回它的名字；提到两个以上就不猜。"""
    matched = [name for name in sorted(set(names), key=len, reverse=True) if name and name in text]
    # "王胖子"和别名"胖子"同时命中时只留长的那个
    longest = [name for name in matched if not any(name != other and name in other for other in matched)]
    if len(longest) != 1:
        return None
    return longest[0]


def from_leading(text: str, names: list[str]) -> str | None:
    """引语前面的旁白：`张卫东歉意的笑道：` / `他低声说`。"""
    head = (text or "").strip()
    if not head.endswith(("：", ":")):
        return None
    window = head[-14:]
    if not SPEECH_CUE.search(window):
        return None
    return _unique_name(window, names)


def from_trailing(text: str, names: list[str]) -> str | None:
    """引语后面的旁白：`王胖子一听，吃惊道。` / `他说。`。"""
    head = (text or "").strip()
    match = ATTRIBUTION_AFTER.match(head[:16])
    if not match:
        return None
    return _unique_name(head[: match.end()], names)


def hints_for_names(units: list, names: list[str]) -> list[str | None]:
    """给每个引语单元找说话人提示（找不到就是 None）。

    先看前一句旁白（`X 说道：“…”` 最常见也最不容易错），再看后一句旁白
    （`“…”X 说道`）。只在前/后旁白里恰好出现一个已知角色时才给提示。
    """
    hints: list[str | None] = [None] * len(units)
    for index, unit in enumerate(units):
        if getattr(unit, "kind", "narration") != "dialogue":
            continue
        previous = units[index - 1] if index else None
        following = units[index + 1] if index + 1 < len(units) else None
        name = None
        if (
            previous is not None
            and previous.kind == "narration"
            and previous.paragraph == unit.paragraph
        ):
            name = from_leading(previous.text, names)
        if (
            name is None
            and following is not None
            and following.kind == "narration"
            and following.paragraph == unit.paragraph
        ):
            name = from_trailing(following.text, names)
        hints[index] = name
    return hints


def hints_for_units(units: list, characters_payload: dict) -> list[str | None]:
    return hints_for_names(units, names_from_payload(characters_payload))
