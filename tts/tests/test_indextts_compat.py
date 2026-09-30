import sys
import threading
import types
from pathlib import Path

from aiab_tts import download as download_module
from aiab_tts.indextts_compat import (
    MODELSCOPE_REPO_ALIASES,
    apply_modelscope_aliases,
    make_gpt_inference_thread_safe,
    modelscope_repo_id,
)

BIGVGAN_HF = "nvidia/bigvgan_v2_22khz_80band_256x"
BIGVGAN_MS = "nv-community/bigvgan_v2_22khz_80band_256x"


def _install_fake_indextts(monkeypatch, mapping=None):
    """塞一个假的 indextts.utils.model_download，检查补丁有没有写进它的映射表。"""
    mapping = {} if mapping is None else mapping
    package = types.ModuleType("indextts")
    utils = types.ModuleType("indextts.utils")
    module = types.ModuleType("indextts.utils.model_download")
    module.HF_TO_MODELSCOPE_REPO_MAP = mapping
    utils.model_download = module
    package.utils = utils
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.utils", utils)
    monkeypatch.setitem(sys.modules, "indextts.utils.model_download", module)
    return mapping


def test_bigvgan_points_at_the_real_modelscope_namespace():
    assert MODELSCOPE_REPO_ALIASES[BIGVGAN_HF] == BIGVGAN_MS
    assert modelscope_repo_id(BIGVGAN_HF) == BIGVGAN_MS
    # 没有别名的仓库不能被动过
    assert modelscope_repo_id("amphion/MaskGCT") == "amphion/MaskGCT"


def test_apply_writes_aliases_into_the_upstream_map(monkeypatch):
    mapping = _install_fake_indextts(
        monkeypatch, {"facebook/w2v-bert-2.0": "AI-ModelScope/w2v-bert-2.0"}
    )
    added = apply_modelscope_aliases()
    assert BIGVGAN_HF in added
    assert mapping[BIGVGAN_HF] == BIGVGAN_MS
    # 上游已有的映射不动
    assert mapping["facebook/w2v-bert-2.0"] == "AI-ModelScope/w2v-bert-2.0"
    # 幂等
    assert apply_modelscope_aliases() == []


def test_apply_is_silent_when_indextts_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "indextts", None)  # import 会失败
    assert apply_modelscope_aliases() == []


def test_apply_is_silent_when_upstream_map_changes_shape(monkeypatch):
    _install_fake_indextts(monkeypatch)
    sys.modules["indextts.utils.model_download"].HF_TO_MODELSCOPE_REPO_MAP = None
    assert apply_modelscope_aliases() == []


def test_fetch_modelscope_translates_huggingface_names(monkeypatch, tmp_path):
    calls: dict = {}

    def fake_snapshot_download(model_id, local_dir=None, revision=None):
        calls.update(model_id=model_id, local_dir=local_dir)
        return local_dir

    modelscope = types.ModuleType("modelscope")
    modelscope.__path__ = []
    hub = types.ModuleType("modelscope.hub")
    hub.__path__ = []
    hub_snapshot = types.ModuleType("modelscope.hub.snapshot_download")
    hub_snapshot.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "modelscope", modelscope)
    monkeypatch.setitem(sys.modules, "modelscope.hub", hub)
    monkeypatch.setitem(sys.modules, "modelscope.hub.snapshot_download", hub_snapshot)

    result = download_module.fetch_modelscope(BIGVGAN_HF, Path(tmp_path))
    assert calls["model_id"] == BIGVGAN_MS
    assert result == Path(tmp_path)


def _install_fake_gpt_inference_model(monkeypatch):
    """塞一个最小的 GPT2InferenceModel：只要 cached_mel_emb 的读写语义。"""

    class FakeGPT2InferenceModel:
        def __init__(self):
            self.cached_mel_emb = None

        def store_mel_emb(self, mel_emb):
            self.cached_mel_emb = mel_emb

    package = types.ModuleType("indextts")
    gpt = types.ModuleType("indextts.gpt")
    module = types.ModuleType("indextts.gpt.model_v2")
    module.GPT2InferenceModel = FakeGPT2InferenceModel
    gpt.model_v2 = module
    package.gpt = gpt
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.gpt", gpt)
    monkeypatch.setitem(sys.modules, "indextts.gpt.model_v2", module)
    return FakeGPT2InferenceModel


def test_gpt_mel_emb_slot_becomes_thread_local(monkeypatch):
    """并发 infer 的根因：cached_mel_emb 是实例级单槽位，后写覆盖先写。

    打好补丁后，每个推理线程各存各的，互不覆盖。
    """
    cls = _install_fake_gpt_inference_model(monkeypatch)
    assert make_gpt_inference_thread_safe() is True
    assert isinstance(cls.cached_mel_emb, property)

    model = cls()
    model.store_mel_emb("主线程的条件嵌入")
    seen: dict[str, str] = {}
    ready = threading.Event()
    release = threading.Event()

    def worker():
        model.store_mel_emb("子线程的条件嵌入")
        seen["worker"] = model.cached_mel_emb
        ready.set()
        release.wait(timeout=5)
        seen["worker_after"] = model.cached_mel_emb

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=5) is True
    # 子线程写入后，主线程读到的仍是自己的值（旧实现会被覆盖成子线程的）
    assert model.cached_mel_emb == "主线程的条件嵌入"
    release.set()
    thread.join(timeout=5)
    assert seen["worker"] == "子线程的条件嵌入"
    assert seen["worker_after"] == "子线程的条件嵌入"


def test_thread_safety_patch_is_idempotent(monkeypatch):
    _install_fake_gpt_inference_model(monkeypatch)
    assert make_gpt_inference_thread_safe() is True
    assert make_gpt_inference_thread_safe() is True


def test_patch_is_silent_when_indextts_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "indextts", None)
    assert make_gpt_inference_thread_safe() is False


def test_mmap_wrapper_injects_mmap_for_big_checkpoints(tmp_path, monkeypatch):
    """大 checkpoint 走 mmap（主机里不再整份读进内存），小文件/显式 mmap 参数不插手。"""
    import torch

    from aiab_tts.indextts_compat import _torch_load_mmap

    calls: list = []

    def fake_load(*args, **kwargs):
        calls.append(kwargs.get("mmap"))
        return "loaded"

    monkeypatch.setattr(torch, "load", fake_load)
    big = tmp_path / "gpt.pth"
    big.write_bytes(b"0" * (17 * 1024 * 1024))
    small = tmp_path / "stat.pth"
    small.write_bytes(b"0" * 128)

    with _torch_load_mmap():
        assert torch.load(str(big), map_location="cpu") == "loaded"
        torch.load(str(small), map_location="cpu")
        torch.load(str(big), map_location="cpu", mmap=False)

    assert calls == [True, None, False]
    assert torch.load is fake_load  # 退出后要还原


def test_mmap_wrapper_falls_back_for_legacy_checkpoints(tmp_path, monkeypatch):
    import torch

    from aiab_tts.indextts_compat import _torch_load_mmap

    big = tmp_path / "legacy.pth"
    big.write_bytes(b"0" * (17 * 1024 * 1024))

    def fake_load(*args, **kwargs):
        if kwargs.get("mmap"):
            raise RuntimeError("legacy serialization")
        return "plain"

    monkeypatch.setattr(torch, "load", fake_load)
    with _torch_load_mmap():
        assert torch.load(str(big), map_location="cpu") == "plain"


def test_wrap_init_on_device_uses_target_device_context():
    """模型构造放进 torch.device 上下文：参数直接建在目标设备上（这里用 meta 验证语义）。"""
    import torch

    from aiab_tts.indextts_compat import _wrap_init_on_device

    class Model:
        def __init__(self):
            self.device = torch.empty(1).device.type

    _wrap_init_on_device(Model, "meta")
    assert Model().device == "meta"
    assert getattr(Model.__init__, "_aiab_device_wrapped", False) is True
    _wrap_init_on_device(Model, "meta")  # 幂等：不能套两层
    assert Model().device == "meta"


def test_fast_model_loading_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AIAB_TTS_FAST_LOAD", "0")
    from aiab_tts.indextts_compat import fast_model_loading

    with fast_model_loading("cuda"):
        pass  # 关闭时必须是个空壳，不能去 import indextts


def test_apply_bf16_modules_casts_weights_and_inputs(monkeypatch):
    """把大模块降 bf16：权重变 bf16，forward 的浮点输入自动跟着转。"""
    import torch

    from aiab_tts.indextts_compat import apply_bf16_modules

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    class FakeModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.w2v = torch.nn.Linear(4, 2)

        def forward(self, x):
            return self.w2v(x)

    class Holder:
        def __init__(self):
            self.semantic_model = FakeModel()

    holder = Holder()
    applied = apply_bf16_modules(holder, use_bf16=True, modules="w2v")

    assert applied == ["w2v"]
    assert next(holder.semantic_model.parameters()).dtype is torch.bfloat16
    out = holder.semantic_model(torch.randn(3, 4))  # fp32 输入也要能跑
    assert out.dtype is torch.bfloat16


def test_apply_bf16_modules_respects_flags(monkeypatch):
    import torch

    from aiab_tts.indextts_compat import apply_bf16_modules

    holder = type("H", (), {"semantic_model": torch.nn.Linear(2, 2)})()
    assert apply_bf16_modules(holder, use_bf16=False) == []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert apply_bf16_modules(holder, use_bf16=True) == []
    assert next(holder.semantic_model.parameters()).dtype is torch.float32


def test_host_memory_report_reads_process_counters():
    from aiab_tts.indextts_compat import host_memory_report

    report = host_memory_report()
    assert report["workingSetMB"] > 0
    assert report["commitMB"] > 0


def test_model_memory_report_flags_weights_left_in_host_memory():
    """自检的核心指标：权重在 CPU 上时 cpuResidentMB 必须报出来（正常应为 0）。"""
    import torch

    from aiab_tts.indextts_compat import model_memory_report

    class Holder(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.semantic_model = torch.nn.Linear(1024, 1024)   # 4MB 权重：够被量化到 MB

    report = model_memory_report(Holder())
    w2v = next(item for item in report["modules"] if item["name"] == "w2v")
    assert w2v["device"] == "cpu" and w2v["dtype"] == "float32"
    assert report["cpuResidentMB"] > 1
    assert report["totalMB"] > 1


def test_model_memory_report_is_zero_when_nothing_is_on_cpu():
    """权重都搬走（这里用 meta 模拟"不在主机内存"）时 cpuResidentMB 归零。"""
    import torch

    from aiab_tts.indextts_compat import model_memory_report

    holder = type("H", (), {"semantic_model": torch.nn.Linear(8, 8, device="meta")})()
    report = model_memory_report(holder)
    assert report["cpuResidentMB"] == 0.0
    assert report["modules"][0]["device"] == "meta"


def test_apply_cfm_speed_overrides_steps_and_cfg():
    """CFM 步数/CFG 是上游写死的：要能在调用时被替换掉。"""
    from aiab_tts.indextts_compat import _wrap_cfm_inference

    seen: list[tuple[int, float]] = []

    class Cfm:
        def inference(self, mu, x_lens, prompt, style, f0, n_timesteps, temperature=1.0, inference_cfg_rate=0.5):
            seen.append((n_timesteps, inference_cfg_rate))
            return "ok"

    model = type("M", (), {"s2mel": type("S", (), {"models": {"cfm": Cfm()}})()})()
    assert _wrap_cfm_inference(model, lambda: {"diffusionSteps": 12, "cfgRate": 0.6}) is True
    out = model.s2mel.models["cfm"].inference("mu", "lens", "prompt", "style", None, 25, inference_cfg_rate=0.7)
    assert out == "ok"
    assert seen == [(12, 0.6)]
    # 幂等：同一个方法不会被套两层
    assert _wrap_cfm_inference(model, lambda: {"diffusionSteps": 8}) is True
    model.s2mel.models["cfm"].inference("mu", "lens", "prompt", "style", None, 25)
    assert seen[-1] == (12, 0.6)


def test_cfm_speed_skips_when_upstream_already_patched(monkeypatch):
    """上游副本自带可调步数时不要再套一层运行时补丁。"""
    from aiab_tts import indextts_compat as compat

    monkeypatch.setattr(compat, "upstream_supports_speed_patch", lambda: True)
    model = type("M", (), {"s2mel": type("S", (), {"models": {"cfm": object()}})()})()
    assert compat.apply_cfm_speed(model, lambda: {}) is True
    assert not hasattr(model.s2mel.models["cfm"], "inference")


def test_upstream_speed_patch_detection_reads_marker(monkeypatch):
    import sys
    import types

    from aiab_tts import indextts_compat as compat

    fake = types.ModuleType("indextts.infer_v2_5")
    fake.AIAB_PATCHED = True
    package = types.ModuleType("indextts")
    package.infer_v2_5 = fake
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", fake)
    assert compat.upstream_supports_speed_patch() is True
    fake.AIAB_PATCHED = False
    assert compat.upstream_supports_speed_patch() is False


def test_speed_tuning_overrides_and_validation(monkeypatch):
    import pytest

    from aiab_tts.config import TtsSettings
    from aiab_tts.indextts_compat import SPEED_TUNING, effective_tuning, set_speed_tuning

    before = dict(SPEED_TUNING)
    try:
        SPEED_TUNING.update({"numBeams": None, "diffusionSteps": None, "cfgRate": None})
        settings = TtsSettings()
        assert effective_tuning(settings) == {"numBeams": 1, "diffusionSteps": 16, "cfgRate": 0.7}
        set_speed_tuning({"numBeams": 3, "diffusionSteps": 25})
        assert effective_tuning(settings)["numBeams"] == 3
        assert effective_tuning(settings)["diffusionSteps"] == 25
        set_speed_tuning({"numBeams": None})
        assert effective_tuning(settings)["numBeams"] == 1
        with pytest.raises(ValueError):
            set_speed_tuning({"numBeams": 0})
        with pytest.raises(ValueError):
            set_speed_tuning({"diffusionSteps": 500})
    finally:
        SPEED_TUNING.update(before)


class _FakeVoiceModel:
    """最小可用的假 IndexTTS2：复刻上游"实例单槽位条件缓存"的读写方式。"""

    def __init__(self, read_delay: float = 0.0):
        self.cache_spk_cond = None
        self.cache_s2mel_style = None
        self.cache_s2mel_prompt = None
        self.cache_spk_audio_prompt = None
        self.cache_mel = None
        self.cache_emo_cond = None
        self.cache_emo_audio_prompt = None
        self.computed: list[str] = []
        self.hits: list[str] = []
        self.mixed = 0
        self.empty_cache_calls = 0
        self.read_delay = read_delay

    def _set_gr_progress(self, value, message=""):
        pass

    def infer_generator(self, spk_audio_prompt, *args, **kwargs):
        import time

        if self.cache_spk_cond is None or self.cache_spk_audio_prompt != spk_audio_prompt:
            if self.cache_spk_cond is not None:
                self.cache_spk_cond = None
                self.empty_cache_calls += 1      # 上游这里真的会调 torch.cuda.empty_cache()
            self.computed.append(spk_audio_prompt)
            self.cache_spk_cond = f"cond:{spk_audio_prompt}"
            self.cache_s2mel_style = f"style:{spk_audio_prompt}"
            self.cache_s2mel_prompt = f"prompt:{spk_audio_prompt}"
            self.cache_mel = f"mel:{spk_audio_prompt}"
            self.cache_spk_audio_prompt = spk_audio_prompt
            self.cache_emo_cond = f"emo:{spk_audio_prompt}"
            self.cache_emo_audio_prompt = spk_audio_prompt
        else:
            self.hits.append(spk_audio_prompt)
        if self.read_delay:
            time.sleep(self.read_delay)
        # 上游紧接着把这些字段抄进局部变量；抄到别人的条件就是串音色
        if self.cache_spk_audio_prompt != spk_audio_prompt:
            self.mixed += 1
            value = "MIXED"
        else:
            value = self.cache_spk_cond
        self._set_gr_progress(0.1, "text processing...")
        yield value


def test_conditioning_cache_is_per_voice():
    from aiab_tts.indextts_compat import make_conditioning_cache_per_voice

    model = _FakeVoiceModel()
    assert make_conditioning_cache_per_voice(model) is True
    assert list(model.infer_generator("voiceA.wav")) == ["cond:voiceA.wav"]
    assert list(model.infer_generator("voiceB.wav")) == ["cond:voiceB.wav"]
    assert list(model.infer_generator("voiceA.wav")) == ["cond:voiceA.wav"]   # 换回来仍然命中
    assert model.computed == ["voiceA.wav", "voiceB.wav"]
    assert model.hits == ["voiceA.wav"]
    assert model.empty_cache_calls == 0        # 不再反复清显存池


def test_conditioning_cache_shim_skips_when_upstream_has_it():
    from aiab_tts.indextts_compat import make_conditioning_cache_per_voice

    model = _FakeVoiceModel()
    model._aiab_voice_cache_native = True
    original = model.infer_generator
    assert make_conditioning_cache_per_voice(model) is True
    # 上游已内置时不再包一层（绑方法每次都是新对象，比的是底层函数有没有被换掉）
    assert model.infer_generator.__func__ is original.__func__


def test_conditioning_cache_does_not_mix_voices_across_threads():
    """并发时 A 在读条件、B 又进来装缓存：老实现会把 B 的条件给 A，串音色。"""
    import concurrent.futures

    from aiab_tts.indextts_compat import make_conditioning_cache_per_voice

    model = _FakeVoiceModel(read_delay=0.05)
    assert make_conditioning_cache_per_voice(model) is True
    voices = ["a.wav", "b.wav", "c.wav", "a.wav", "b.wav", "c.wav"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda voice: list(model.infer_generator(voice)), voices))
    assert model.mixed == 0
    assert results == [[f"cond:{voice}"] for voice in voices]
