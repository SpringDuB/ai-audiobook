import time
from pathlib import Path

from . import store
from .analysis.models import DELIVERIES, EMOTIONS, clamp01

EDITABLE_FIELDS = ("text", "speaker", "addressee", "emotion", "intensity", "delivery")


def _role_name(names: dict[str, str], role_id: str) -> str:
    """names 的约定是 {role_id: 角色名}（与 characters.json / API 一致）。"""
    return names.get(role_id, role_id)


def _resolve_role(target: str, names: dict[str, str]) -> str | None:
    """接受 role_id 或角色名，统一返回 role_id。"""
    if target in names:
        return target
    return next((role_id for role_id, name in names.items() if name == target), None)


def apply_line_patch(row: dict, patch: dict, names: dict[str, str]) -> dict:
    """人工修改一行标注：字段白名单 + 取值校验 + 上限保护。"""
    unknown = sorted(set(patch) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(f"不可修改的字段：{', '.join(unknown)}")
    updated = dict(row)
    if "text" in patch:
        text = str(patch["text"]).strip()
        if not text:
            raise ValueError("台词不能为空")
        updated["text"] = text
    if "speaker" in patch:
        speaker = str(patch["speaker"])
        role_id = _resolve_role(speaker, names)
        if not role_id:
            raise ValueError(f"未知说话人：{speaker}")
        updated["speaker"] = role_id
        updated["speaker_name"] = _role_name(names, role_id)
    if "addressee" in patch:
        target = str(patch["addressee"] or "")
        if not target:
            updated["addressee"] = None
            updated["addressee_name"] = None
        else:
            role_id = _resolve_role(target, names)
            if not role_id:
                raise ValueError(f"未知受话人：{target}")
            updated["addressee"] = role_id
            updated["addressee_name"] = _role_name(names, role_id)
    if "emotion" in patch or "intensity" in patch:
        dominant = str(patch.get("emotion") or (updated.get("emotion") or {}).get("dominant") or "平静")
        if dominant not in EMOTIONS:
            raise ValueError(f"未知情绪：{dominant}")
        fallback = (updated.get("emotion") or {}).get("intensity")
        if not isinstance(fallback, (int, float)) or fallback <= 0:
            # 旁白的 intensity 是 0（不带情绪）；人工给它加情绪时用中位数兜底
            fallback = 0.5
        raw = patch.get("intensity", fallback)
        updated["emotion"] = {
            "dominant": dominant,
            "intensity": round(clamp01(float(raw)), 3),
            "source": "manual",
        }
    if "delivery" in patch:
        delivery = str(patch["delivery"])
        if delivery not in DELIVERIES:
            raise ValueError(f"未知语气：{delivery}")
        updated["delivery"] = delivery
    updated["edited_at"] = int(time.time() * 1000)
    return updated


def invalidate_chapter(settings, book_id: str, index: int) -> bool:
    """人工改动后让该章成品失效：删 render.json 与容器，post 会按新输入重渲染。"""
    removed = False
    for path in (
        store.chapter_render_meta_path(settings, book_id, index),
        store.chapter_media_path(settings, book_id, index, "mkv"),
        store.chapter_media_path(settings, book_id, index, "mp4"),
    ):
        if Path(path).exists():
            Path(path).unlink()
            removed = True
    return removed
