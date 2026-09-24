from pathlib import Path

import httpx
import pytest
from helpers import make_voice, wav_bytes

from audiobook import audio
from audiobook.engines.base import SynthParams
from audiobook.engines.http_tts import HttpTtsEngine
from audiobook.text.chunking import chunk_text


def _caps(max_text_chars: int = 300) -> dict:
    return {
        "engine": "indextts-2.5",
        "engineVersion": "2.5.0",
        "emotions": True,
        "emotionDims": ["happy"],
        "rate": True,
        "pronunciation": True,
        "sampleRate": 22050,
        "maxTextChars": max_text_chars,
    }


def _engine(settings, handler) -> HttpTtsEngine:
    return HttpTtsEngine("http://tts.local", settings, transport=httpx.MockTransport(handler))


def test_chunk_text_is_shared_with_analysis_layer():
    assert chunk_text("第一句。第二句。第三句。", 8) == ["第一句。第二句。", "第三句。"]


def test_short_text_uses_single_request(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=_caps())
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.2), headers={"X-Duration-Sec": "0.2"})

    settings = settings.model_copy(update={"tts_max_line_chunk_chars": 100})
    result = _engine(settings, handler).synthesize("短句。", "v_test", SynthParams(), Path(settings.data_dir) / "o.wav")
    assert calls["synth"] == 1
    assert result.duration == pytest.approx(0.2, abs=1e-3)


def test_long_text_is_split_concatenated_and_cleaned_up(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=_caps())
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.2), headers={"X-Duration-Sec": "0.2"})

    settings = settings.model_copy(update={"tts_max_line_chunk_chars": 8})
    out = Path(settings.data_dir) / "out.wav"
    result = _engine(settings, handler).synthesize("第一句。第二句。第三句。", "v_test", SynthParams(), out)

    assert calls["synth"] == 2  # 8 字上限 → 2 块
    assert result.duration == pytest.approx(0.52, abs=1e-3)  # 0.2 + 120ms + 0.2
    assert audio.wav_duration(out) == pytest.approx(0.52, abs=1e-3)
    assert list(out.parent.glob("*.part*.wav")) == []


def test_chunk_limit_falls_back_to_server_max_text_chars(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=_caps(max_text_chars=6))
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.1), headers={"X-Duration-Sec": "0.1"})

    engine = _engine(settings, handler)  # tts_max_line_chunk_chars 默认 0 → 用服务端 6
    engine.synthesize("第一句。第二句。", "v_test", SynthParams(), Path(settings.data_dir) / "out.wav")
    assert calls["synth"] == 2
