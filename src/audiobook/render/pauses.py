from ..text.prosody import derive_pause_ms


def effective_pause_ms(row: dict, next_row: dict | None, settings) -> int:
    """导出时重算停顿：以文本与情绪强度为准，支持逐行覆盖与全局缩放。"""
    override = row.get("pause_override_ms")
    if override is not None:
        base = int(override)
    else:
        emotion = row.get("emotion") or {}
        base = derive_pause_ms(row.get("text") or "", intensity=emotion.get("intensity"))
    scaled = int(round(base * settings.pause_scale))
    return max(settings.pause_min_ms, min(settings.pause_max_ms, scaled))


def build_pause_plan(rows: list[dict], settings) -> list[int]:
    plan = []
    for position, row in enumerate(rows):
        next_row = rows[position + 1] if position + 1 < len(rows) else None
        plan.append(effective_pause_ms(row, next_row, settings))
    return plan
