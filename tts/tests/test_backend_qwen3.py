"""qwen3 后端：按描述生成（VoiceDesign）与参考音频克隆（Base）两条路。

不加载真模型：用假的 qwen_tts 模块替身，只验"参数有没有传对、分支有没有走对"。
"""

import io
import sys
import types
import wave
from pathlib import Path

import pytest

from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.qwen3 import Qwen3TtsBackend, language_name
from aiab_tts.config import TtsSettings


class _FakeModel:
    """替身模型：记录调用参数，吐一段固定长度的正弦 WAV。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.design_calls: list[dict] = []
        self.clone_calls: list[dict] = []
        self.prompt_calls: list[dict] = []

    def generate_voice_design(self, *, text, language, instruct):
        self.design_calls.append({"text": text, "language": language, "instruct": instruct})
        texts = text if isinstance(text, list) else [text]
        return [self._wave(0.2) for _ in texts], 24000

    def create_voice_clone_prompt(self, *, ref_audio, ref_text, x_vector_only_mode=False):
        self.prompt_calls.append(
            {"ref_audio": ref_audio, "ref_text": ref_text, "x_vector_only_mode": x_vector_only_mode}
        )
        return {"prompt": Path(ref_audio).name}

    def generate_voice_clone(self, *, text, language, voice_clone_prompt):
        self.clone_calls.append({"text": text, "language": language, "prompt": voice_clone_prompt})
        texts = text if isinstance(text, list) else [text]
        return [self._wave(0.2) for _ in texts], 24000

    @staticmethod
    def _wave(seconds: float):
        import numpy as np

        return np.zeros(int(24000 * seconds), dtype="float32")


@pytest.fixture()
def fake_qwen(monkeypatch, tmp_path):
    """装一个假的 qwen_tts 模块，并把两个权重目录准备好（只校验 config.json 存在）。"""
    models: dict[str, _FakeModel] = {}

    class _FakeQwen3TTSModel:
        @staticmethod
        def from_pretrained(path, **kwargs):
            kind = "base" if path.endswith("Base") else "design"
            models.setdefault(kind, _FakeModel(kind))
            return models[kind]

    module = types.ModuleType("qwen_tts")
    module.Qwen3TTSModel = _FakeQwen3TTSModel
    monkeypatch.setitem(sys.modules, "qwen_tts", module)

    for name in ("Qwen3-TTS-12Hz-1.7B-Base", "Qwen3-TTS-12Hz-1.7B-VoiceDesign"):
        target = tmp_path / name
        target.mkdir(parents=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
    settings = TtsSettings(model_dir=tmp_path, model_source="local", use_bf16=False)
    return Qwen3TtsBackend(settings), models


def _wav_seconds(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload)) as handle:
        return handle.getnframes() / float(handle.getframerate())


def test_language_name_maps_pipeline_codes():
    assert language_name("ZH") == "Chinese"
    assert language_name("jp") == "Japanese"
    assert language_name("") == "Auto"
    assert language_name("XX") == "Auto"


def test_synthesize_with_voice_prompt_goes_through_voice_design(fake_qwen):
    backend, models = fake_qwen
    result = backend.synthesize(
        SynthesisRequest(text="你先坐下。", voice_prompt="低沉的男声。这一句：平稳地陈述。", lang="ZH")
    )
    assert _wav_seconds(result.audio) == pytest.approx(0.2, abs=0.01)
    assert result.sample_rate == 24000
    design = models["design"]
    assert design.design_calls == [
        {"text": "你先坐下。", "language": "Chinese", "instruct": "低沉的男声。这一句：平稳地陈述。"}
    ]
    assert "base" not in models  # 纯描述不该加载克隆模型


def test_batch_design_passes_every_line_in_one_call(fake_qwen):
    backend, models = fake_qwen
    requests = [
        SynthesisRequest(text="第一句。", voice_prompt="年轻女声。这一句：轻快。", lang="ZH"),
        SynthesisRequest(text="第二句。", voice_prompt="年轻女声。这一句：压低声音。", lang="ZH"),
    ]
    paths = [Path(backend.settings.model_dir) / f"{index}.wav" for index in range(2)]
    durations = backend.synthesize_batch(requests, paths)
    assert len(durations) == 2 and all(path.exists() for path in paths)
    calls = models["design"].design_calls
    assert len(calls) == 1
    assert calls[0]["text"] == ["第一句。", "第二句。"]
    assert calls[0]["instruct"] == ["年轻女声。这一句：轻快。", "年轻女声。这一句：压低声音。"]


def test_reference_audio_switches_to_clone_model(fake_qwen, tmp_path):
    backend, models = fake_qwen
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFfake")
    result = backend.synthesize(
        SynthesisRequest(text="克隆我。", ref_path=ref, ref_text="克隆我。", lang="ZH")
    )
    assert _wav_seconds(result.audio) == pytest.approx(0.2, abs=0.01)
    assert models["base"].clone_calls[0]["prompt"] == {"prompt": "ref.wav"}
    assert models["base"].prompt_calls[0]["x_vector_only_mode"] is False
    # 同一音色第二次合成复用 clone prompt，不重复算特征
    backend.synthesize(SynthesisRequest(text="再来一句。", ref_path=ref, ref_text="克隆我。", lang="ZH"))
    assert len(models["base"].prompt_calls) == 1


def test_design_endpoint_returns_wav_and_text(fake_qwen):
    backend, _models = fake_qwen
    audio, sample_rate, spoken = backend.design(text="你先坐下。", instruct="低沉的男声。")
    assert sample_rate == 24000
    assert spoken == "你先坐下。"
    assert audio[:4] == b"RIFF"
