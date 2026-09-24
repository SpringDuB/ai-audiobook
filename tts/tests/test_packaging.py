"""守住 pyproject：真后端的依赖必须声明在项目里，而不是靠一串手工命令。"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _data() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_no_fake_backend_in_description():
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "fake" not in text.split("[project.optional-dependencies]")[0]


def test_indextts_extra_declares_the_inference_stack():
    extra = _data()["project"]["optional-dependencies"]["indextts"]
    joined = " ".join(extra)
    for package in ("torch", "torchaudio", "transformers", "librosa", "soundfile", "numpy", "sentencepiece", "cn2an"):
        assert package in joined, f"indextts extra 缺少 {package}"
    assert "torch==2.8.*" in joined and "transformers==4.52.1" in joined      # 跟 index-tts 的 pin 对齐


def test_download_extra_declares_both_clients():
    extra = _data()["project"]["optional-dependencies"]["download"]
    joined = " ".join(extra)
    assert "modelscope" in joined and "huggingface_hub" in joined


def test_base_dependencies_stay_lightweight():
    """基础依赖里不该出现 torch —— 没 GPU 的机器也能装、能跑测试。"""
    base = " ".join(_data()["project"]["dependencies"])
    assert "torch" not in base
