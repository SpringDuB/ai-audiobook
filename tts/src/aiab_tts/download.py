import hashlib
import json
import os
from pathlib import Path

from .indextts_compat import modelscope_repo_id

MANIFEST_NAME = "manifest.json"
AUX_DIR_MARKER = "hf_cache/"


class ModelIntegrityError(RuntimeError):
    """权重缺失或哈希不符。"""


def is_auxiliary(name: str) -> bool:
    """IndexTTS-2.5 的辅助模型都落在 checkpoints/hf_cache/ 下（w2v-bert、MaskGCT、CAMPPlus、BigVGAN）。"""
    normalized = name.replace("\\", "/").lower()
    return normalized.startswith(AUX_DIR_MARKER) or f"/{AUX_DIR_MARKER}" in normalized


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(directory: Path) -> dict[str, dict]:
    path = Path(directory) / MANIFEST_NAME
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def verify_files(directory: Path, manifest: dict[str, dict]) -> dict:
    report: dict[str, list[str]] = {"ok": [], "missing": [], "mismatch": []}
    for name, expected in sorted(manifest.items()):
        path = Path(directory) / name
        if not path.exists():
            report["missing"].append(name)
            continue
        if expected.get("size") is not None and path.stat().st_size != expected["size"]:
            report["mismatch"].append(name)
            continue
        if expected.get("sha256") and sha256_file(path) != expected["sha256"]:
            report["mismatch"].append(name)
            continue
        report["ok"].append(name)
    return report


def split_report(report: dict) -> tuple[list[str], list[str], list[str]]:
    """把校验报告拆成（主权重问题、辅助模型缺口、警告文本）。"""
    main_gaps = [name for name in report["missing"] + report["mismatch"] if not is_auxiliary(name)]
    aux_gaps = [name for name in report["missing"] if is_auxiliary(name)]
    warnings = [f"辅助模型未就绪: {name}（首次推理时 IndexTTS 会自行下载）" for name in aux_gaps]
    return main_gaps, aux_gaps, warnings


def fetch_modelscope(model_id: str, target_dir: Path, revision: str | None = None) -> Path:
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError as exc:  # pragma: no cover - 只在真实下载时需要
        raise RuntimeError("未安装 modelscope：请先 uv pip install modelscope，或把 model_source 改成 local") from exc
    # 同一个别名表：HF 名字在 ModelScope 上叫别的名字时（如 BigVGAN）换成真名
    snapshot_download(modelscope_repo_id(model_id), local_dir=str(target_dir), revision=revision)
    return Path(target_dir)


def fetch_huggingface(model_id: str, target_dir: Path, hf_endpoint: str = "") -> Path:
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 huggingface_hub：请先 uv pip install huggingface_hub，或把 model_source 改成 local") from exc
    snapshot_download(repo_id=model_id, local_dir=str(target_dir))
    return Path(target_dir)


def ensure_model(settings, fetcher=None) -> dict:
    """模型来源三选一 + 校验 + 断点续传。返回 {"path", "source", "verified", "warnings"}。"""
    target = Path(settings.model_dir)
    target.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(target)
    report = verify_files(target, manifest) if manifest else {"ok": [], "missing": [], "mismatch": []}
    main_gaps, aux_gaps, warnings = split_report(report)
    source = (settings.model_source or "local").lower()

    if source == "local":
        if manifest and main_gaps and settings.verify_manifest:
            raise ModelIntegrityError(f"本地模型校验失败，缺失或损坏: {main_gaps}")
        return {
            "path": target,
            "source": source,
            "verified": bool(manifest) and not main_gaps,
            "warnings": warnings,
        }

    if manifest and not main_gaps:
        return {"path": target, "source": source, "verified": True, "warnings": warnings}

    if not settings.allow_download:
        raise ModelIntegrityError("allow_download=false 但模型不完整")
    if fetcher is None:
        if source == "modelscope":
            fetcher = lambda model_id, directory: fetch_modelscope(model_id, directory)  # noqa: E731
        elif source == "huggingface":
            fetcher = lambda model_id, directory: fetch_huggingface(model_id, directory, settings.hf_endpoint)  # noqa: E731
        else:
            raise ValueError(f"未知模型来源: {settings.model_source}")
    fetcher(settings.model_id, target)

    report = verify_files(target, manifest)
    main_gaps, _, warnings = split_report(report)
    if manifest and main_gaps:
        raise ModelIntegrityError(f"下载后校验失败，缺失或损坏: {main_gaps}")
    return {"path": target, "source": source, "verified": True, "warnings": warnings}
