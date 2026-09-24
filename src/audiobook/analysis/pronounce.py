import json

from .. import store


def load_pronounce_table(settings) -> dict[str, str]:
    """读取用户维护的注音词表（data/pronounce.json）；缺失或损坏都当空表。"""
    path = store.pronounce_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key).strip(): str(value).strip() for key, value in data.items() if str(key).strip()}


def match_pronunciations(text: str, table: dict[str, str]) -> dict[str, str]:
    """只注入词表命中的词 —— 不让模型自由编造注音。"""
    return {word: reading for word, reading in sorted(table.items()) if word and word in text}
