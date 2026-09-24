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
        weight = float(intensity) if isinstance(intensity, (int, float)) else 1.0
        vector = tuple(round(weight, 4) if dim == dominant else 0.0 for dim in caps.emotion_dims)
    return SynthParams(
        emo_vector=vector,
        rate=float(row.get("rate") or 1.0),
        lang=row.get("lang") or "ZH",
        pronunciation=row.get("pronounce") or None,
    )
