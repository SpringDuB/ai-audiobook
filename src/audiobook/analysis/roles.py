"""角色表的小工具：名字/别名 → role_id 的解析与查询。"""

from .models import NARRATOR_NAMES, is_narrator

NARRATOR_ID = "narrator"


def normalize(name: str | None) -> str:
    return (name or "").strip().replace(" ", "")


def characters_index(payload: dict) -> dict[str, dict]:
    return {character["id"]: character for character in payload.get("characters") or []}


def resolve_speaker(payload: dict, name: str) -> str | None:
    """按本名或别名找 role_id；旁白的各种写法统一到 narrator。"""
    if is_narrator(name):
        return NARRATOR_ID
    target = normalize(name)
    if not target:
        return None
    for character in payload.get("characters") or []:
        if target == normalize(character.get("name")):
            return character["id"]
        if any(target == normalize(alias) for alias in character.get("aliases") or []):
            return character["id"]
    return None


def role_name(payload: dict, role_id: str) -> str:
    for character in payload.get("characters") or []:
        if character["id"] == role_id:
            return character.get("name") or role_id
    return role_id


def names_from_payload(payload: dict) -> list[str]:
    """角色表里的本名 + 别名（本名在前）；旁白按固定写法补上。"""
    names: list[str] = []
    for character in payload.get("characters") or []:
        primary = (character.get("name") or "").strip()
        if primary and primary not in names:
            names.append(primary)
        for alias in character.get("aliases") or []:
            alias = (alias or "").strip()
            if alias and alias not in names:
                names.append(alias)
    if any(character.get("is_narrator") for character in payload.get("characters") or []):
        for narrator_name in NARRATOR_NAMES:
            if narrator_name not in names:
                names.append(narrator_name)
    return names


def next_role_number(characters: list[dict]) -> int:
    numbers = [
        int(str(item.get("id", "")).rsplit("_", 1)[-1])
        for item in characters
        if str(item.get("id", "")).startswith("role_") and str(item.get("id", "")).rsplit("_", 1)[-1].isdigit()
    ]
    return max(numbers or [0])
