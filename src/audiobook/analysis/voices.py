"""这一行要怎么说：角色基础音色描述 + 本句表演描述 → 合成用的 VoicePlan。

角色音色不再依赖参考音频：
  - **角色基础描述**（年龄/性别/音色质地/说话习惯/气质）由大模型按角色写，存在
    casting.json 里，用户能改、能让模型重写；
  - **本句表演描述**（语气/情绪/语速/音量/气息）由提取阶段的大模型逐句直出，
    落在行记录的 voice_prompt 上；
  - 合成时两段拼成一句交给 Qwen3-TTS 的 VoiceDesign（generate_voice_design）。

这样每条台词都按自己的语气演，不会被一段参考音频的固定语气"传染"（IndexTTS 的老毛病）。
代价是同一角色的音色会有轻微浮动 —— 这是明确接受的取舍。

参考音频（库存音色）仍然保留成一条可选通道：角色被手工绑定库存音色时走克隆。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .. import store
from .roles import NARRATOR_ID

# 描述缺失时的兜底（正常流程里不该出现：分析链会先把描述补齐）
FALLBACK_DESCRIPTION = "自然清晰的声音，语速适中，语气平稳"
FALLBACK_LINE = "平静地陈述"


@dataclass(frozen=True)
class VoicePlan:
    """一行音频要用的音色：缓存身份 + 喂给 TTS 的那句描述。"""

    voice_key: str  # 缓存键身份：design:<role_id>:<描述指纹> / lib:<voice_id>
    voice_id: str  # 传给引擎的 id（库存音色按它找 ref.wav）
    instruct: str  # 拼好的音色描述（角色基础 + 本句）
    lang: str
    source: str  # design | library | design-missing
    ref_path: Path | None = None
    ref_text: str = ""


def description_key(description: str) -> str:
    """基础描述的指纹：描述一改，这个角色所有行的缓存键就变了（只重跑他一个人）。"""
    text = (description or "").strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def compose_instruct(description: str, line_prompt: str) -> str:
    """角色基础描述 + 本句表演描述 → 一句完整的音色指令。"""
    base = (description or "").strip().rstrip("。；;")
    line = (line_prompt or "").strip().rstrip("。；;")
    if not base and not line:
        return FALLBACK_DESCRIPTION + "。" + FALLBACK_LINE + "。"
    if not line:
        return base + "。"
    if not base:
        return f"{FALLBACK_DESCRIPTION}。这一句：{line}。"
    return f"{base}。这一句：{line}。"


def role_of(casting: dict, speaker: str) -> dict:
    return (casting.get("roles") or {}).get(speaker) or {}


def base_description(role: dict) -> str:
    return str(role.get("description") or "").strip()


def resolve_line_voice(settings, book_id: str, row: dict, casting: dict | None = None) -> VoicePlan:
    """把一行解析成合成计划：默认按描述生成，手工绑了库存音色才走克隆。"""
    casting = casting if casting is not None else (
        store.read_json(store.casting_path(settings, book_id), default={}) or {}
    )
    speaker = str(row.get("speaker") or "")
    role = role_of(casting, speaker)
    line_prompt = str(row.get("voice_prompt") or "").strip()
    lang = str(row.get("lang") or "ZH")

    voice_source = role.get("voice_source")
    if voice_source is None:
        # 老 casting.json：手选/旁白算库存音色，其余按设计音色处理
        voice_source = "library" if role.get("source") == "manual" else "design"
    if voice_source == "library" and role.get("voice_id") and role.get("voice_id") != "default":
        return VoicePlan(
            voice_key=f"lib:{role['voice_id']}",
            voice_id=str(role["voice_id"]),
            instruct=compose_instruct(base_description(role), line_prompt),
            lang=lang,
            source="library",
        )

    description = base_description(role)
    if description:
        return VoicePlan(
            voice_key=f"design:{speaker}:{description_key(description)}",
            voice_id=speaker or NARRATOR_ID,
            instruct=compose_instruct(description, line_prompt),
            lang=lang,
            source="design",
        )
    # 还没设计：先用兜底描述把音频出出来（前端会显示"未设计"，分析链会补）
    return VoicePlan(
        voice_key=f"design:{speaker}:missing",
        voice_id=speaker or NARRATOR_ID,
        instruct=compose_instruct("", line_prompt),
        lang=lang,
        source="design-missing",
    )


# ---------------------------------------------------------------- 试听产物（角色卡片用）

def preview_path(settings, book_id: str, role_id: str) -> Path:
    return store.role_voice_dir(settings, book_id, role_id) / "preview.wav"


def preview_ready(settings, book_id: str, role_id: str, description: str) -> bool:
    """试听音频在不在、是不是按当前描述生成的（描述改了就作废）。"""
    path = preview_path(settings, book_id, role_id)
    if not path.exists():
        return False
    meta = store.read_json(path.with_suffix(".json"), default={}) or {}
    return meta.get("description_key") == description_key(description)


def write_preview_meta(settings, book_id: str, role_id: str, payload: dict) -> None:
    store.atomic_replace_json(preview_path(settings, book_id, role_id).with_suffix(".json"), payload)
