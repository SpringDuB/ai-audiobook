import io
import sys
import types
import wave

import pytest
from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.indextts import IndexTtsBackend, _assert_python, apply_pronunciation
from aiab_tts.config import TtsSettings


def _fake_wav(path, seconds: float = 0.25, rate: int = 22050) -> None:
    frames = int(rate * seconds)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    with open(path, "wb") as handle:
        handle.write(stream.getvalue())


@pytest.fixture()
def fake_indextts(monkeypatch):
    calls: dict = {}

    class FakeIndexTTS2:
        def __init__(self, cfg_path=None, model_dir=None, use_bf16=True):
            calls["init"] = {"cfg_path": cfg_path, "model_dir": model_dir, "use_bf16": use_bf16}

        def infer(self, **kwargs):
            calls["infer"] = kwargs
            _fake_wav(kwargs["output_path"])
            return kwargs["output_path"]

    module = types.ModuleType("indextts.infer_v2_5")
    module.IndexTTS2 = FakeIndexTTS2
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)
    monkeypatch.setattr(sys, "version_info", (3, 11, 9))
    return calls


def test_apply_pronunciation_prefers_longest_word():
    mapping = {"重": "CHONG2", "重复": "CHONG2FU4"}
    assert apply_pronunciation("重复一次，重来。", mapping) == "<重复|CHONG2FU4>一次，<重|CHONG2>来。"
    assert apply_pronunciation("没事", {}) == "没事"


def test_python_guard_rejects_313_with_actionable_message():
    _assert_python((3, 11))
    _assert_python((3, 10))
    with pytest.raises(RuntimeError) as excinfo:
        _assert_python((3, 13))
    assert "3.10" in str(excinfo.value) and "uv python install 3.11" in str(excinfo.value)


def test_load_passes_paths_and_bf16(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path, use_bf16=False)
    backend = IndexTtsBackend(settings)
    backend.load()
    assert fake_indextts["init"]["model_dir"] == str(tmp_path)
    assert fake_indextts["init"]["cfg_path"].endswith("config.yaml")
    assert fake_indextts["init"]["use_bf16"] is False
    assert backend.is_loaded() is True


def test_synthesize_maps_rate_to_duration_factor(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path)
    backend = IndexTtsBackend(settings)
    backend.load()
    request = SynthesisRequest(
        text="银<行|XING2>里", ref_path=tmp_path / "ref.wav", ref_text="参考",
        lang="ZH", emo_vector=(0, 0, 0, 0, 0, 0, 0, 0.8), rate=1.25,
        pronunciation={"行": "XING2"}, seed=11,
    )
    result = backend.synthesize(request)
    assert fake_indextts["infer"]["duration_factor"] == pytest.approx(0.8)
    assert fake_indextts["infer"]["emo_vector"] == [0, 0, 0, 0, 0, 0, 0, 0.8]
    assert fake_indextts["infer"]["lang"] == "ZH"
    assert fake_indextts["infer"]["spk_audio_prompt"] == str(tmp_path / "ref.wav")
    assert fake_indextts["infer"]["text"] == "银<行|XING2>里"
    assert result.duration_sec == pytest.approx(0.25, abs=1e-3)
    assert result.audio[:4] == b"RIFF"
    assert result.engine == "indextts-2.5"


def test_recommended_concurrency_prefers_explicit_setting(tmp_path, monkeypatch):
    explicit = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=2))
    assert explicit.recommended_concurrency() == 2

    auto = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=0))
    monkeypatch.setattr("aiab_tts.backends.indextts._vram_total_mb", lambda settings: 24000)
    assert auto.recommended_concurrency() == 3
    monkeypatch.setattr("aiab_tts.backends.indextts._vram_total_mb", lambda settings: 12000)
    assert auto.recommended_concurrency() == 2
    monkeypatch.setattr("aiab_tts.backends.indextts._vram_total_mb", lambda settings: 6000)
    assert auto.recommended_concurrency() == 1


def test_capabilities_describe_indextts_2_5(fake_indextts, tmp_path):
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    caps = backend.capabilities()
    assert caps["engine"] == "indextts-2.5"
    assert caps["emotionDims"][0] == "happy"
    assert caps["languages"] == ["ZH", "EN", "JP", "ES", "AR"]
    assert caps["pronunciationStyles"] == ["pinyin", "cmu", "kana"]


def test_unload_releases_model(fake_indextts, tmp_path):
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    backend.load()
    backend.unload()
    assert backend.is_loaded() is False
