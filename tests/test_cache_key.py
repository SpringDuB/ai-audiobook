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
