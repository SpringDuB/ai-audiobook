"""code_predictor 快路径的单测（不需要 GPU / 不需要真模型）。"""

from __future__ import annotations

import pytest

pytest.importorskip("torch")

import torch  # noqa: E402

from aiab_tts.backends.qwen3_fast import (  # noqa: E402
    _capture_window,
    _PredictorOutput,
    _sample,
    capture_active,
    install_fast_predictor,
    uninstall_fast_predictor,
)


def test_capture_window_is_globally_visible():
    """捕获窗口必须全局可见：state 的空闲归还据此跳过 empty_cache，否则
    多路并发时会撞出 "operation not permitted when stream is capturing"。"""
    assert capture_active() is False
    with _capture_window():
        assert capture_active() is True
    assert capture_active() is False


def test_empty_cache_gives_way_to_capture(monkeypatch):
    from aiab_tts import state

    calls: list[int] = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: calls.append(1))

    with _capture_window():
        state._empty_cuda_cache()  # 捕获窗口里必须跳过
    assert calls == []

    state._empty_cuda_cache()  # 窗口外照常归还
    assert calls == [1]


def test_greedy_is_argmax():
    logits = torch.tensor([[0.1, 3.0, 0.2]])
    picked = _sample(logits, do_sample=False, top_k=50, top_p=1.0, temperature=0.9)
    assert picked.tolist() == [[1]]


def test_top_k_one_matches_argmax():
    logits = torch.tensor([[0.1, 5.0, 0.2, 4.9]])
    picked = _sample(logits, do_sample=True, top_k=1, top_p=1.0, temperature=1.0)
    assert picked.tolist() == [[1]]


def test_nucleus_never_picks_outside_top_p():
    # 只有一个位置概率占绝对多数时，top_p=0.5 必须只能抽到它
    logits = torch.tensor([[100.0, 0.0, 0.0, 0.0]])
    for _ in range(8):
        picked = _sample(logits, do_sample=True, top_k=0, top_p=0.5, temperature=1.0)
        assert picked.tolist() == [[0]]


def test_sample_respects_shape_and_device():
    logits = torch.randn(4, 32)
    picked = _sample(logits, do_sample=True, top_k=8, top_p=0.95, temperature=0.7)
    assert picked.shape == (4, 1)
    assert picked.dtype == torch.int64


class _FakePredictor:
    def __init__(self):
        self.calls = 0

    def generate(self, **kwargs):  # 上游实现
        self.calls += 1
        return _PredictorOutput(torch.zeros(1, 15, dtype=torch.int64))


class _FakeTalker:
    def __init__(self):
        self.code_predictor = _FakePredictor()


class _FakeModel:
    def __init__(self):
        self.talker = _FakeTalker()


def test_install_and_uninstall_restores_upstream():
    model = _FakeModel()
    original = model.talker.code_predictor.generate
    text = install_fast_predictor(model, mode="loop")
    assert "loop" in text
    assert model.talker.code_predictor.generate != original

    uninstall_fast_predictor(model)
    assert model.talker.code_predictor.generate == original


def test_unknown_mode_rejected():
    model = _FakeModel()
    with pytest.raises(ValueError):
        install_fast_predictor(model, mode="turbo")
