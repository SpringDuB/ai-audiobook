import hashlib
import json

from .engines.base import EngineCapabilities, SynthParams, summarize_params


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


def params_from_line(row: dict, caps: EngineCapabilities) -> SynthParams:
    emotion = row.get("emotion") or {}
    vector = None
    dominant = emotion.get("dominant")
    if caps.emotions and dominant in caps.emotion_dims:
        intensity = emotion.get("intensity")
        weights = {dim: 0.0 for dim in caps.emotion_dims}
        # mix = 主情绪 + 副情绪（同一句话里的两层情绪），直接拼成 IndexTTS 的 8 维向量
        mix = emotion.get("mix") or [
            {"name": dominant, "weight": intensity if isinstance(intensity, (int, float)) else 1.0}
        ]
        for item in mix:
            name = (item or {}).get("name")
            weight = (item or {}).get("weight")
            if name in weights and isinstance(weight, (int, float)):
                weights[name] = max(weights[name], float(weight))
        vector = tuple(round(min(1.0, max(0.0, weights[dim])), 4) for dim in caps.emotion_dims)
    return SynthParams(
        emo_vector=vector,
        rate=float(row.get("rate") or 1.0),
        lang=row.get("lang") or "ZH",
        pronunciation=row.get("pronounce") or None,
    )
