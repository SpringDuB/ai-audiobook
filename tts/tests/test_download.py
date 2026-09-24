import hashlib
import json
from pathlib import Path

import pytest

from aiab_tts.config import TtsSettings
from aiab_tts.download import (
    ModelIntegrityError,
    ensure_model,
    is_auxiliary,
    load_manifest,
    sha256_file,
    verify_files,
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _manifest_for(directory: Path, names: list[str]) -> dict:
    return {
        name: {"size": (directory / name).stat().st_size, "sha256": sha256_file(directory / name)}
        for name in names
    }


def test_verify_files_reports_ok_missing_and_mismatch(tmp_path):
    _write(tmp_path / "a.bin", b"aaa")
    _write(tmp_path / "b.bin", b"bbb")
    manifest = {
        "a.bin": {"size": 3, "sha256": hashlib.sha256(b"aaa").hexdigest()},
        "b.bin": {"size": 3, "sha256": hashlib.sha256(b"wrong").hexdigest()},
        "c.bin": {"size": 3, "sha256": hashlib.sha256(b"ccc").hexdigest()},
    }
    report = verify_files(tmp_path, manifest)
    assert report == {"ok": ["a.bin"], "missing": ["c.bin"], "mismatch": ["b.bin"]}


def test_auxiliary_detection_uses_hf_cache_directory():
    assert is_auxiliary("hf_cache/models--facebook--w2v-bert-2.0/pytorch_model.bin") is True
    assert is_auxiliary("gpt.pth") is False
    assert is_auxiliary("bigvgan/config.json") is False


def test_local_source_verifies_without_downloading(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "config.yaml", b"cfg")
    _write(model_dir / "gpt.pth", b"weights")
    (model_dir / "manifest.json").write_text(
        json.dumps(_manifest_for(model_dir, ["config.yaml", "gpt.pth"])), encoding="utf-8"
    )
    settings = TtsSettings(backend="fake", model_source="local", model_dir=model_dir)
    result = ensure_model(settings)
    assert result["verified"] is True
    assert result["source"] == "local"
    assert result["warnings"] == []


def test_local_source_raises_on_tampered_weight(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "gpt.pth", b"weights")
    (model_dir / "manifest.json").write_text(json.dumps(_manifest_for(model_dir, ["gpt.pth"])), encoding="utf-8")
    _write(model_dir / "gpt.pth", b"tampered")
    settings = TtsSettings(model_source="local", model_dir=model_dir)
    with pytest.raises(ModelIntegrityError):
        ensure_model(settings)


def test_hub_source_uses_fetcher_and_skips_when_complete(tmp_path):
    hub = tmp_path / "hub"
    _write(hub / "config.yaml", b"cfg")
    _write(hub / "gpt.pth", b"weights")
    target = tmp_path / "checkpoints"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps(
            {
                name: {"size": (hub / name).stat().st_size, "sha256": sha256_file(hub / name)}
                for name in ("config.yaml", "gpt.pth")
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def fetcher(model_id: str, target_dir: Path) -> Path:
        calls["n"] += 1
        for name in ("config.yaml", "gpt.pth"):
            _write(target_dir / name, (hub / name).read_bytes())
        return target_dir

    settings = TtsSettings(model_source="modelscope", model_dir=target)
    first = ensure_model(settings, fetcher=fetcher)
    assert calls["n"] == 1 and first["verified"] is True
    second = ensure_model(settings, fetcher=fetcher)
    assert calls["n"] == 1  # 已完整 → 不再下载
    assert second["verified"] is True


def test_auxiliary_model_gap_is_a_warning_not_an_error(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "gpt.pth", b"weights")
    manifest = _manifest_for(model_dir, ["gpt.pth"])
    manifest["hf_cache/models--facebook--w2v-bert-2.0/pytorch_model.bin"] = {"size": 1, "sha256": "x" * 64}
    (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    settings = TtsSettings(model_source="local", model_dir=model_dir)
    result = ensure_model(settings)
    assert result["verified"] is True
    assert any("w2v-bert" in warning for warning in result["warnings"])
    assert load_manifest(model_dir)["gpt.pth"]["size"] == 7
