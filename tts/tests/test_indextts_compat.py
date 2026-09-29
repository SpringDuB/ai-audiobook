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
