import sys
import types
from pathlib import Path

from aiab_tts import download as download_module
from aiab_tts.indextts_compat import (
    MODELSCOPE_REPO_ALIASES,
    apply_modelscope_aliases,
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
