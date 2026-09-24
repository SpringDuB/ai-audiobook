import json
from pathlib import Path

import httpx
import pytest
from helpers import make_voice, wav_bytes

from audiobook.engines.base import SynthParams
from audiobook.engines.errors import TtsBusy, TtsOom, TtsVoiceMissing
from audiobook.engines.http_tts import HttpTtsEngine

CAPS = {
    "engine": "indextts-2.5",
    "engineVersion": "2.5.0",
    "emotions": True,
    "emotionDims": ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"],
    "rate": True,
    "rateRange": [0.5, 2.0],
    "pronunciation": True,
    "pronunciationStyles": ["pinyin"],
    "languages": ["ZH"],
    "sampleRate": 22050,
    "maxTextChars": 300,
}


def engine_with(handler, settings, **kwargs) -> HttpTtsEngine:
    return HttpTtsEngine("http://tts.local", settings, transport=httpx.MockTransport(handler), **kwargs)


def with_caps(handler):
    """给测试 handler 补上 /capabilities 分支：引擎在合成前要读 maxTextChars。"""

    def wrapper(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=CAPS)
        return handler(request)

    return wrapper


def test_capabilities_are_mapped_and_cached(settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/capabilities"
        return httpx.Response(200, json=CAPS)

    engine = engine_with(handler, settings)
    caps = engine.capabilities()
    assert caps.name == "indextts-2.5" and caps.version == "2.5.0"
    assert caps.emotion_dims[0] == "happy" and caps.sample_rate == 22050
    assert caps.max_text_chars == 300
    engine.capabilities()
    assert calls["n"] == 1


def test_uploads_reference_once_and_reuses_ref_id(settings):
    make_voice(settings)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1", "durationSec": 6.0, "sampleRate": 22050})
        return httpx.Response(
            200,
            content=wav_bytes(),
            headers={"X-Duration-Sec": "0.1", "X-Sample-Rate": "22050", "X-Engine-Version": "2.5.0"},
        )

    engine = engine_with(with_caps(handler), settings)
    out = Path(settings.data_dir) / "a.wav"
    engine.synthesize("第一句。", "v_test", SynthParams(), out)
    engine.synthesize("第二句。", "v_test", SynthParams(), out)
    assert seen.count("/v1/refs") == 1
    assert seen.count("/v1/synthesize") == 2
    assert out.exists()


def test_synthesize_payload_carries_params(settings):
    make_voice(settings)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        if request.url.path == "/v1/synthesize":
            captured.update(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes(), headers={"X-Duration-Sec": "0.1"})

    engine = engine_with(with_caps(handler), settings)
    engine.synthesize(
        "你重说一遍！",
        "v_test",
        SynthParams(emo_vector=(0, 0, 0, 0, 0, 0, 0, 0.9), rate=1.05, lang="ZH", pronunciation={"重": "CHONG2"}),
        Path(settings.data_dir) / "a.wav",
    )
    assert captured["refId"] == "ref_1"
    assert captured["emoVector"][7] == 0.9
    assert captured["rate"] == 1.05
    assert captured["lang"] == "ZH"
    assert captured["pronunciation"] == {"重": "CHONG2"}
    assert captured["format"] == "wav"


def test_bad_ref_triggers_reupload_and_retry(settings):
    make_voice(settings)
    state = {"refs": 0, "synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            state["refs"] += 1
            return httpx.Response(200, json={"refId": f"ref_{state['refs']}"})
        state["synth"] += 1
        if state["synth"] == 1:
            return httpx.Response(404, json={"detail": {"code": "bad_ref", "message": "unknown refId"}})
        return httpx.Response(200, content=wav_bytes(), headers={"X-Duration-Sec": "0.1"})

    engine = engine_with(with_caps(handler), settings)
    engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")
    assert state == {"refs": 2, "synth": 2}


def test_missing_voice_file_raises_voice_missing(settings):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("不应发起请求")

    engine = engine_with(with_caps(handler), settings)
    with pytest.raises(TtsVoiceMissing):
        engine.synthesize("第一句。", "v_missing", SynthParams(), Path(settings.data_dir) / "a.wav")


def test_service_errors_map_to_typed_exceptions(settings):
    make_voice(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(503, json={"detail": {"code": "oom", "message": "CUDA out of memory"}})

    engine = engine_with(with_caps(handler), settings)
    with pytest.raises(TtsOom):
        engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")

    def busy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(429, text="slow down")

    with pytest.raises(TtsBusy):
        engine_with(with_caps(busy), settings).synthesize(
            "第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "b.wav"
        )


def test_duration_falls_back_to_wav_header(settings):
    make_voice(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(200, content=wav_bytes(seconds=0.5))

    engine = engine_with(with_caps(handler), settings)
    result = engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")
    assert result.duration == pytest.approx(0.5, abs=1e-3)
    assert result.sample_rate == 22050
