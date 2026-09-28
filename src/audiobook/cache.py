import hashlib
import json

from .engines.base import EngineCapabilities, SynthParams, dim_name_for, summarize_params


def cache_key(text: str, voice_id: str, caps: EngineCapabilities, params: SynthParams | None) -> str:
    payload = {
        "text": text,
        "voice": voice_id,
        "engine": caps.name,
        "engine_version": caps.version,
        "params": summarize_params(params, caps),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def params_from_line(row: dict, caps: EngineCapabilities, mode: str = "text") -> SynthParams:
    """把一行分析结果转成合成参数。

    mode = text（默认）：用 emotion_text 这句自然语言描述驱动情绪；
    mode = vector：用 emotion.mix 拼出的 8 维向量。
    服务端没加载 QwenEmotion（caps.emotion_text=False）时，文本模式自动退回向量。
    """
    emotion = row.get("emotion") or {}
    emotion_text = str(row.get("emotion_text") or "").strip()
    use_text = (mode or "text").lower() == "text" and caps.emotion_text and bool(emotion_text)
    vector = None
    dominant = dim_name_for(emotion.get("dominant"), caps.emotion_dims)
    if not use_text and caps.emotions and dominant in caps.emotion_dims:
        intensity = emotion.get("intensity")
        weights = {dim: 0.0 for dim in caps.emotion_dims}
        # mix = 主情绪 + 副情绪（同一句话里的两层情绪），直接拼成 IndexTTS 的 8 维向量
        mix = emotion.get("mix") or [
            {"name": dominant, "weight": intensity if isinstance(intensity, (int, float)) else 1.0}
        ]
        for item in mix:
            name = dim_name_for((item or {}).get("name"), caps.emotion_dims)
            weight = (item or {}).get("weight")
            if name in weights and isinstance(weight, (int, float)):
                weights[name] = max(weights[name], float(weight))
        vector = tuple(round(min(1.0, max(0.0, weights[dim])), 4) for dim in caps.emotion_dims)
    return SynthParams(
        emo_vector=vector,
        emotion_text=emotion_text if use_text else None,
        rate=float(row.get("rate") or 1.0),
        lang=row.get("lang") or "ZH",
        pronunciation=row.get("pronounce") or None,
    )
