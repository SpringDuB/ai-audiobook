"""启动 TTS 服务时的模型准备：先确认依赖装好，再 ensure_model（必要时下载），最后加载。"""

import sys
import types
import logging

import pytest
from aiab_tts.backends.indextts import IndexTtsBackend
from aiab_tts.cli import describe_backend
from aiab_tts.config import TtsSettings


def test_unknown_backend_is_rejected():
    """产品里只有 indextts 一种后端，别再有"假引擎"这种选项。"""
    from aiab_tts.app import build_backend

    with pytest.raises(ValueError, match="只支持 indextts"):
        build_backend(TtsSettings(backend="fake"))


def test_indextts_backend_says_it_will_download():
    lines = describe_backend(TtsSettings(backend="indextts", model_source="modelscope"))
    assert any("模型来源=modelscope" in line for line in lines)
    assert any("自动下载" in line for line in lines)
    local = describe_backend(TtsSettings(backend="indextts", model_source="local"))
    assert any("不下载" in line for line in local)


def test_load_prepares_model_after_the_dependency_import(tmp_path, monkeypatch):
    """先确认依赖装好，再去下几个 GB 的权重；顺序反了会白下一轮。"""
    order: list[str] = []

    def fake_ensure_model(settings):
        order.append(f"ensure:{settings.model_source}")
        return {"path": tmp_path, "source": settings.model_source, "verified": True, "warnings": ["辅助模型未就绪"]}

    monkeypatch.setattr("aiab_tts.download.ensure_model", fake_ensure_model)
    module = types.ModuleType("indextts.infer_v2_5")

    class FakeIndexTTS2:
        def __init__(self, cfg_path=None, model_dir=None, use_bf16=True, use_qwen_emo=False):
            order.append("construct")

    module.IndexTTS2 = FakeIndexTTS2
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)

    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, model_source="modelscope"))
    backend.load()
    assert order == ["ensure:modelscope", "construct"]


def test_load_reports_missing_local_weights_clearly(tmp_path, monkeypatch, caplog):
    from aiab_tts import download as download_module

    def broken(settings):  # noqa: ARG001
        raise download_module.ModelIntegrityError("本地模型校验失败，缺失或损坏: ['gpt.pth']")

    monkeypatch.setattr("aiab_tts.download.ensure_model", broken)
    module = types.ModuleType("indextts.infer_v2_5")
    module.IndexTTS2 = object
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, model_source="local"))
    with pytest.raises(RuntimeError) as excinfo:
        backend.load()
    assert "模型不可用" in str(excinfo.value) and "gpt.pth" in str(excinfo.value)


def test_load_warns_when_torch_is_cpu_only(tmp_path, monkeypatch, caplog):
    """CPU 版 torch 会静默退回 CPU 推理：要在日志里点名，并给出装 CUDA 版的命令。"""
    fake_torch = types.ModuleType("torch")
    fake_torch.__version__ = "2.8.0+cpu"
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr("aiab_tts.download.ensure_model", lambda settings: {
        "path": tmp_path, "source": settings.model_source, "verified": True, "warnings": [],
    })
    module = types.ModuleType("indextts.infer_v2_5")
    module.IndexTTS2 = type("IndexTTS2", (), {"__init__": lambda self, **kwargs: None})
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)

    with caplog.at_level(logging.WARNING, logger="aiab_tts.backends.indextts"):
        IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path)).load()
    assert any("看不到 CUDA" in record.message and "2.8.0+cpu" in record.getMessage() for record in caplog.records)
    assert any("download.pytorch.org/whl/cu128" in record.getMessage() for record in caplog.records)
