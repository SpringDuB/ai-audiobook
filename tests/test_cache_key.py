import pytest

from audiobook.cache import cache_key
from audiobook.engines.base import EngineCapabilities, SynthParams


def _caps(**overrides) -> EngineCapabilities:
    base = dict(
        name="fake",
        version="fake-1",
        emotions=True,
        emotion_dims=("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"),
        rate=True,
        pronunciation=True,
        sample_rate=22050,
        max_text_chars=300,
    )
    base.update(overrides)
    return EngineCapabilities(**base)


def test_key_changes_with_text_voice_and_engine_version():
    caps = _caps()
    calm = SynthParams(emo_vector=(0, 0, 0, 0, 0, 0, 0, 1))
    base = cache_key("你好", "v1", caps, calm)
    assert base != cache_key("你好。", "v1", caps, calm)
    assert base != cache_key("你好", "v2", caps, calm)
    assert base != cache_key("你好", "v1", caps, SynthParams(emo_vector=(0, 0, 0, 0, 0, 0, 1, 0)))
    assert base != cache_key("你好", "v1", _caps(version="fake-2"), calm)


def test_key_ignores_params_the_engine_does_not_support():
    caps = _caps(emotions=False, pronunciation=False)
    a = cache_key(
        "你好", "v1", caps,
        SynthParams(emo_vector=(0, 0, 0, 0, 0, 0, 0, 1), pronunciation={"行": "XING2"}),
    )
    b = cache_key(
        "你好", "v1", caps,
        SynthParams(emo_vector=(1, 0, 0, 0, 0, 0, 0, 0), pronunciation={"行": "HANG2"}),
    )
    assert a == b


def test_key_changes_with_rate_when_supported():
    caps = _caps()
    assert cache_key("你好", "v1", caps, SynthParams(rate=1.0)) != cache_key("你好", "v1", caps, SynthParams(rate=0.92))


def test_emotion_text_participates_in_the_cache_key():
    """切到文本描述通道后必须重合成：两种通道的缓存键不同。"""
    caps = _caps(emotion_text=True)
    text_mode = SynthParams(emotion_text="压着火气，语速比平时快")
    vector_mode = SynthParams(emo_vector=(0, 0.9, 0, 0, 0, 0, 0, 0))
    assert cache_key("你给我住手！", "v1", caps, text_mode) != cache_key("你给我住手！", "v1", caps, vector_mode)
    assert cache_key("你给我住手！", "v1", caps, text_mode) != cache_key(
        "你给我住手！", "v1", caps, SynthParams(emotion_text="疲惫但温柔")
    )


def test_params_from_line_switches_channel():
    from audiobook.cache import params_from_line

    row = {
        "text": "你给我住手！",
        "emotion": {"dominant": "愤怒", "intensity": 0.9, "mix": [{"name": "愤怒", "weight": 0.9}]},
        "emotion_text": "压着火气，语速比平时快",
        "rate": 1.05,
        "lang": "ZH",
    }
    text_params = params_from_line(row, _caps(emotion_text=True), mode="text")
    assert text_params.emotion_text == "压着火气，语速比平时快" and text_params.emo_vector is None

    vector_params = params_from_line(row, _caps(emotion_text=True), mode="vector")
    # 向量总强度会被抬到 0.9~1.0（见 cache.normalize_emotion_weights）：
    # 不这样的话 (1-Σ) 这一项会让参考音频自己的语气占掉大半
    assert vector_params.emotion_text is None
    assert vector_params.emo_vector[1] == pytest.approx(0.99)
    assert 0.9 <= sum(vector_params.emo_vector) <= 1.0

    # 服务端不支持文本描述时：文本模式也要退回向量
    fallback = params_from_line(row, _caps(emotion_text=False), mode="text")
    assert fallback.emotion_text is None and fallback.emo_vector[1] == pytest.approx(0.99)


def test_emotion_weights_are_normalized_to_full_strength():
    """Σ 权重统一抬到 0.9~1.0：参考音频的情绪占比从 40%+ 降到 ≤10%。"""
    from audiobook.cache import normalize_emotion_weights

    dims = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")

    weak = {dim: 0.0 for dim in dims}
    weak["calm"] = 0.4
    out = normalize_emotion_weights(weak)
    assert out["calm"] == pytest.approx(0.94)  # Σ=0.4 → 抬到 0.94
    assert 0.9 <= sum(out.values()) <= 1.0

    # Σ>1（主情绪 1.0 + 副情绪 0.45）会被压回 1.0 以内，避免 (1-Σ) 变成负数
    strong = {dim: 0.0 for dim in dims}
    strong["angry"] = 1.0
    strong["sad"] = 0.45
    squeezed = normalize_emotion_weights(strong)
    assert sum(squeezed.values()) == pytest.approx(1.0)
    assert squeezed["angry"] > squeezed["sad"]

    empty = normalize_emotion_weights({dim: 0.0 for dim in dims})
    assert sum(empty.values()) == 0.0


def test_chinese_emotion_names_map_to_engine_dims():
    """回归：中文情绪名必须翻成引擎维度名，否则 2477 个片段的 emo_vector 全是 None。"""
    from audiobook.cache import params_from_line
    from audiobook.engines.base import dim_name_for

    dims = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")
    assert dim_name_for("愤怒", dims) == "angry"
    assert dim_name_for("平静", dims) == "calm"
    assert dim_name_for("不存在的情绪", dims) is None
    assert dim_name_for("愤怒", ()) is None

    row = {
        "text": "他停住了。",
        "emotion": {
            "dominant": "悲伤",
            "intensity": 0.7,
            "mix": [{"name": "悲伤", "weight": 0.7}, {"name": "平静", "weight": 0.3}],
        },
    }
    params = params_from_line(row, _caps(), mode="vector")
    assert params.emo_vector[2] == 0.7   # sad
    assert params.emo_vector[7] == 0.3   # calm
