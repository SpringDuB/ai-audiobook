"""落盘：把「说话人名字 + 本句表演描述」物化成行记录（id / role_id / 语速 / 注音）。

大模型给的是名字（还可能给"未知"），这里把它翻成 role_id，并把旁白与人物话术分开：
对不上角色表的说话人会写进异常清单，不允许静默降级。
"""

from .derive import derive_line
from .models import SpokenLine, is_narrator
from .roles import NARRATOR_ID, characters_index, resolve_speaker, role_name


def materialize(
    *,
    chapter_index: int,
    spoken: list[SpokenLine],
    characters_payload: dict,
    pronounce_table: dict[str, str],
    scene_index: int = 1,
) -> tuple[list[dict], list[dict]]:
    index = characters_index(characters_payload)
    narrator_name = index.get(NARRATOR_ID, {}).get("name") or "旁白"
    rows: list[dict] = []
    unknown: list[str] = []
    for item in spoken:
        text = (item.text or "").strip()
        if not text:
            continue
        role = (item.role or "").strip() or "未知"
        kind = "narration" if is_narrator(role) else "dialogue"
        speaker_id = NARRATOR_ID
        speaker_name = narrator_name
        if kind == "dialogue":
            resolved = resolve_speaker(characters_payload, role)
            if resolved is None:
                unknown.append(role)
            else:
                speaker_id = resolved
                speaker_name = role_name(characters_payload, resolved)
        row = derive_line(
            {
                "emotion": item.emotion,
                "intensity": item.intensity,
                "secondary": item.secondary,
                "secondary_weight": item.secondary_weight,
            },
            chapter_index=chapter_index,
            scene_index=scene_index,
            seq=len(rows) + 1,
            sentence=text,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            kind=kind,
            pronounce_table=pronounce_table,
            voice_prompt=(item.voice or "").strip(),
        )
        rows.append(row)
    issues: list[dict] = []
    if unknown:
        distinct = list(dict.fromkeys(unknown))
        issues.append(
            {
                "kind": "unknown_speaker",
                "reason": f"{len(unknown)} 句的说话人不在角色表里：{'、'.join(distinct[:5])}",
                "fallback": "按旁白音色处理",
                "detail": {"count": len(unknown), "roles": distinct[:10]},
            }
        )
    return rows, issues
