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


# 情绪向量总强度（Σ 权重）：IndexTTS 那边是
#   每句情绪 = Σwᵢ·情绪ᵢ + (1 - Σw)·参考音频自己的情绪
# 分析结果里 Σ 中位只有 0.6，等于每句话有 40% 的语气仍然来自参考音频，
# 听感就是"音色像、语气也像参考，情绪拉不开"。这里统一抬到 0.9~1.0：
# 既让参考语气让位（≥90% 由这句话的情绪决定），又保留一点点强弱层次。
EMOTION_TOTAL_MIN = 0.9
EMOTION_TOTAL_MAX = 1.0


def normalize_emotion_weights(weights: dict[str, float]) -> dict[str, float]:
    """把 8 维情绪权重按比例缩放到 Σ∈[0.9, 1.0]；每维上限 1.0。

    Σ 本身也是"这句话情绪有多确定"的信号，所以不是一刀切拉满：
    Σ=0.2 的行落到 0.92，Σ=0.6 落到 0.96，Σ≥1 落到 1.0。
    """
    total = sum(weights.values())
    if total <= 0:
        return dict(weights)
    target = EMOTION_TOTAL_MIN + (EMOTION_TOTAL_MAX - EMOTION_TOTAL_MIN) * min(1.0, total)
    factor = target / total
    return {dim: min(1.0, value * factor) for dim, value in weights.items()}


def params_from_line(
    row: dict,
    caps: EngineCapabilities,
    mode: str = "text",
    *,
    voice_prompt: str = "",
    ref_path=None,
    ref_text: str = "",
) -> SynthParams:
    """把一行分析结果转成合成参数。

    mode = text（默认）：用 emotion_text 这句自然语言描述驱动情绪；
    mode = vector：用 emotion.mix 拼出的 8 维向量。
    服务端没加载 QwenEmotion（caps.emotion_text=False）时，文本模式自动退回向量。
    旁白（kind=narration）不带情绪：只出文本，不拼向量。

    ref_path/ref_text：这一行的参考音频（设计音色专用）。Qwen3-TTS 不接受任何情绪
    参数，caps 里 emotions/emotion_text 都是 False —— 那两段自然就不会被拼进去。
    """
    emotion = row.get("emotion") or {}
    emotion_text = str(row.get("emotion_text") or "").strip()
    narration = (row.get("kind") or "dialogue") == "narration"
    use_text = (mode or "text").lower() == "text" and caps.emotion_text and bool(emotion_text) and not narration
    vector = None
    dominant = dim_name_for(emotion.get("dominant"), caps.emotion_dims)
    if not narration and not use_text and caps.emotions and dominant in caps.emotion_dims:
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
        weights = normalize_emotion_weights(weights)
        vector = tuple(round(min(1.0, max(0.0, weights[dim])), 4) for dim in caps.emotion_dims)
    return SynthParams(
        emo_vector=vector,
        emotion_text=emotion_text if use_text else None,
        rate=float(row.get("rate") or 1.0),
        lang=row.get("lang") or "ZH",
        pronunciation=row.get("pronounce") or None,
        voice_prompt=voice_prompt,
        ref_path=ref_path,
        ref_text=ref_text,
    )
