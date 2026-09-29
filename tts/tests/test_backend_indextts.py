import io
import sys
import types
import wave

import pytest
from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.indextts import IndexTtsBackend, apply_pronunciation
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
        def __init__(self, cfg_path=None, model_dir=None, use_bf16=True, use_qwen_emo=False):
            calls["init"] = {
                "cfg_path": cfg_path,
                "model_dir": model_dir,
                "use_bf16": use_bf16,
                "use_qwen_emo": use_qwen_emo,
            }

        def infer(self, **kwargs):
            calls["infer"] = kwargs
            _fake_wav(kwargs["output_path"])
            return kwargs["output_path"]

    module = types.ModuleType("indextts.infer_v2_5")
    module.IndexTTS2 = FakeIndexTTS2
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    # 上游的 HF→ModelScope 别名表：load() 里应该被我们补上 BigVGAN
    utils = types.ModuleType("indextts.utils")
    model_download = types.ModuleType("indextts.utils.model_download")
    model_download.HF_TO_MODELSCOPE_REPO_MAP = {}
    utils.model_download = model_download
    package.utils = utils

    # GPT 推理模型的并发补丁目标（load() 里会往它上面装线程本地 property）
    gpt = types.ModuleType("indextts.gpt")
    model_v2 = types.ModuleType("indextts.gpt.model_v2")

    class FakeGPT2InferenceModel:
        def __init__(self):
            self.cached_mel_emb = None

        def store_mel_emb(self, mel_emb):
            self.cached_mel_emb = mel_emb

    model_v2.GPT2InferenceModel = FakeGPT2InferenceModel
    gpt.model_v2 = model_v2
    package.gpt = gpt
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)
    monkeypatch.setitem(sys.modules, "indextts.utils", utils)
    monkeypatch.setitem(sys.modules, "indextts.utils.model_download", model_download)
    monkeypatch.setitem(sys.modules, "indextts.gpt", gpt)
    monkeypatch.setitem(sys.modules, "indextts.gpt.model_v2", model_v2)
    monkeypatch.setattr(sys, "version_info", (3, 11, 9))
    calls["repo_map"] = model_download.HF_TO_MODELSCOPE_REPO_MAP
    return calls


def test_apply_pronunciation_prefers_longest_word():
    mapping = {"重": "CHONG2", "重复": "CHONG2FU4"}
    assert apply_pronunciation("重复一次，重来。", mapping) == "<重复|CHONG2FU4>一次，<重|CHONG2>来。"
    assert apply_pronunciation("没事", {}) == "没事"


def test_load_only_depends_on_the_installed_package(monkeypatch, tmp_path):
    """不卡 Python 版本：能不能跑只取决于 index-tts 是否装得上。"""
    monkeypatch.setattr(sys, "version_info", (3, 13, 5))
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, model_source="local"))
    with pytest.raises(RuntimeError) as excinfo:
        backend.load()
    assert "IndexTTS 不可用" in str(excinfo.value)
    # 报错要给出装法（3.11 只是 index-tts 自己的声明，不是我们拦的）
    assert "index-tts" in str(excinfo.value) and "uv pip install" in str(excinfo.value)


def test_load_passes_paths_and_bf16(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path, use_bf16=False)
    backend = IndexTtsBackend(settings)
    backend.load()
    assert fake_indextts["init"]["model_dir"] == str(tmp_path)
    assert fake_indextts["init"]["cfg_path"].endswith("config.yaml")
    assert fake_indextts["init"]["use_bf16"] is False
    assert backend.is_loaded() is True


def test_load_enables_qwen_emotion_when_configured(fake_indextts, tmp_path):
    """文本描述情绪通道要加载 QwenEmotion；没开就只走 8 维向量。"""
    off = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, use_qwen_emo=False))
    off.load()
    assert fake_indextts["init"]["use_qwen_emo"] is False
    assert off.capabilities()["emotionText"] is False

    on = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, use_qwen_emo=True))
    on.load()
    assert fake_indextts["init"]["use_qwen_emo"] is True
    assert on.capabilities()["emotionText"] is True


def test_synthesize_prefers_emotion_text_when_available(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path, use_qwen_emo=True)
    backend = IndexTtsBackend(settings)
    backend.load()
    request = SynthesisRequest(
        text="你给我住手！", ref_path=tmp_path / "ref.wav", lang="ZH",
        emo_vector=(0, 0.9, 0, 0, 0, 0, 0, 0), emotion_text="压着火气，语速比平时快", rate=1.0,
    )
    backend.synthesize(request)
    assert fake_indextts["infer"]["use_emo_text"] is True
    assert fake_indextts["infer"]["emo_text"] == "压着火气，语速比平时快"
    assert fake_indextts["infer"]["emo_vector"] is None


def test_synthesize_falls_back_to_vector_without_qwen(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path, use_qwen_emo=False)
    backend = IndexTtsBackend(settings)
    backend.load()
    request = SynthesisRequest(
        text="你给我住手！", ref_path=tmp_path / "ref.wav", lang="ZH",
        emo_vector=(0, 0.9, 0, 0, 0, 0, 0, 0), emotion_text="压着火气",
    )
    backend.synthesize(request)
    assert fake_indextts["infer"]["use_emo_text"] is False
    assert fake_indextts["infer"]["emo_text"] is None
    assert fake_indextts["infer"]["emo_vector"] == [0, 0.9, 0, 0, 0, 0, 0, 0]


def test_load_patches_modelscope_bigvgan_alias(fake_indextts, tmp_path):
    """BigVGAN 在 ModelScope 上叫 nv-community/...，加载前必须把别名补进去。"""
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    backend.load()
    assert (
        fake_indextts["repo_map"]["nvidia/bigvgan_v2_22khz_80band_256x"]
        == "nv-community/bigvgan_v2_22khz_80band_256x"
    )


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


def test_recommended_concurrency_honors_setting_with_default_three(tmp_path):
    """默认 3 路并发；显式配置优先；只有补丁打不上时才退回 1（宁可慢也不能算错）。"""
    default = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    assert default.recommended_concurrency() == 3

    explicit = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=5))
    assert explicit.recommended_concurrency() == 5

    degraded = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=3))
    degraded._thread_safe = False
    assert degraded.recommended_concurrency() == 1


def test_synthesize_serializes_model_access(fake_indextts, tmp_path):
    """load() 必须给 GPT 推理模型打上线程本地补丁（并发安全的根因修复）。"""
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    backend.load()  # 用假模型，不加载真实权重

    from indextts.gpt.model_v2 import GPT2InferenceModel

    assert isinstance(GPT2InferenceModel.cached_mel_emb, property)
    assert backend.recommended_concurrency() == 3


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


def test_synthesize_seeds_torch_when_seed_given(fake_indextts, tmp_path, monkeypatch):
    """传 seed 时先给 torch 播种：同一 seed + 同一输入可复现（A/B 对比要用）。"""
    import torch

    calls = []
    monkeypatch.setattr(torch, "manual_seed", lambda value: calls.append(("cpu", value)))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "manual_seed_all", lambda value: calls.append(("cuda", value)))

    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    backend.load()
    request = SynthesisRequest(text="播种测试。", ref_path=tmp_path / "ref.wav", lang="ZH", seed=20260929)
    backend.synthesize(request)

    assert calls == [("cpu", 20260929), ("cuda", 20260929)]
    assert backend.capabilities()["supportsSeed"] is True
