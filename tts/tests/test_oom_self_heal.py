"""OOM 请求内自愈：先只留模型权重把显存还回去，再把批量包减半重试。

契约（来自线上故障复盘）：
  1. 请求边界的清理失败绝不能顶掉请求结果（那会把一次显存抖动放大成 500 + 熔断）；
  2. 8 条一包 OOM → 还显存 → 4 条重试；单条仍 OOM → 按句切分再拼回一条；
  3. 自愈只作用于本次请求，成功一次就立刻回到整包，不留跨请求的降档记忆。
"""

import io
import wave
import zipfile

import pytest

from _stub_backend import StubBackend
from aiab_tts.config import TtsSettings
from aiab_tts.state import (
    ServiceError,
    ServiceState,
    _concat_wav,
    is_oom_error,
    split_text_for_retry,
)

OOM_MESSAGE = "CUDA out of memory. Tried to allocate 2.00 GiB (GPU 0; 8.00 GiB total capacity)"


def _state(tmp_path, backend=None, **overrides) -> ServiceState:
    settings = TtsSettings(data_dir=tmp_path / "data", oom_retry_wait_seconds=0.0, **overrides)
    return ServiceState(backend or StubBackend(), settings)


def _wav_bytes(seconds: float = 0.05, sample_rate: int = 22050) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


class PackBackend(StubBackend):
    """批量接口：包超过 oom_over 条就抛 OOM（模拟"8 条长台词一起解码吃不下"）。"""

    def __init__(self, oom_over: int = 4):
        super().__init__()
        self.oom_over = oom_over
        self.batch_sizes: list[int] = []

    def synthesize_batch(self, requests, out_paths):
        self.batch_sizes.append(len(requests))
        if len(requests) > self.oom_over:
            raise RuntimeError(OOM_MESSAGE)
        durations = []
        for path in out_paths:
            path.write_bytes(_wav_bytes())
            durations.append(0.05)
        return durations


class LengthOomBackend(StubBackend):
    """台词超过 max_len 字就 OOM：验证单条失败后按句切分。"""

    def __init__(self, max_len: int = 12):
        super().__init__()
        self.max_len = max_len
        self.texts: list[str] = []  # 尝试过的（含失败）
        self.ok_texts: list[str] = []  # 真正合成成功的

    def synthesize(self, request):
        self.texts.append(request.text)
        if len(request.text) > self.max_len:
            raise RuntimeError(OOM_MESSAGE)
        self.ok_texts.append(request.text)
        return super().synthesize(request)


class DeadBackend(StubBackend):
    """怎么救都 OOM：用来确认最终仍然是 503 + code=oom（客户端据此降档，而不是判死端点）。"""

    def synthesize(self, request):
        raise RuntimeError(OOM_MESSAGE)


def test_clear_cuda_graphs_walks_to_the_installed_graph_pool(tmp_path):
    """OOM 自愈要靠清图池把常驻显存还回去；属性路径写错会静默失效，所以这里对着真实安装点验。"""
    pytest.importorskip("torch")
    types = __import__("types")

    from aiab_tts.backends.qwen3_fast import install_fast_predictor
    from aiab_tts.backends.qwen3 import Qwen3TtsBackend

    predictor = types.SimpleNamespace(generate=lambda **kwargs: None)
    inner = types.SimpleNamespace(talker=types.SimpleNamespace(code_predictor=predictor))
    install_fast_predictor(inner, mode="graph")

    backend = Qwen3TtsBackend(TtsSettings(model_dir=tmp_path))
    backend._models["design"] = types.SimpleNamespace(model=inner)

    assert backend.clear_cuda_graphs() == 1
    assert predictor._aiab_graph_generators._runners == {}
    assert backend.clear_cuda_graphs() == 1  # 再清一次也不能炸
    assert Qwen3TtsBackend(TtsSettings(model_dir=tmp_path)).clear_cuda_graphs() == 0  # 没模型时安全


def test_is_oom_error_recognizes_torch_driver_and_plain_messages():
    class OutOfMemoryError(RuntimeError):  # 与 torch.cuda.OutOfMemoryError 同名
        pass

    assert is_oom_error(OutOfMemoryError("boom"))
    assert is_oom_error(RuntimeError("CUDA error: out of memory"))
    assert is_oom_error(RuntimeError("CUDA_ERROR_OUT_OF_MEMORY"))
    assert not is_oom_error(RuntimeError("illegal memory access was encountered"))
    assert not is_oom_error(ValueError("bad text"))


def test_split_text_for_retry_keeps_every_character():
    text = "第一句话很长很长。第二句话也很长很长。第三句话同样很长。"
    pieces = split_text_for_retry(text)
    assert len(pieces) == 2
    assert "".join(pieces) == text

    hard = split_text_for_retry("没有标点的一长串文字")
    assert len(hard) == 2 and "".join(hard) == "没有标点的一长串文字"

    assert split_text_for_retry("短") == ["短"]


def test_concat_wav_joins_durations():
    audio, duration = _concat_wav([_wav_bytes(0.05), _wav_bytes(0.10)])
    assert duration == pytest.approx(0.15, abs=0.01)
    with wave.open(io.BytesIO(audio)) as handle:
        assert handle.getnframes() == pytest.approx(0.15 * 22050, abs=220)


def test_cleanup_failure_does_not_fail_the_request(tmp_path):
    """回归：清理时 empty_cache 抛 CUDA OOM 曾经让整个请求变成 500。"""
    state = _state(tmp_path)
    state.warmup()

    def boom() -> None:
        raise RuntimeError("CUDA error: out of memory")

    state.release_after_request = boom
    result = state.synthesize({"text": "第一句。", "voicePrompt": "青年男声"})
    assert result.audio
    assert state.inflight == 0


def test_batch_oom_halves_pack_and_restores_full_pack(tmp_path):
    backend = PackBackend(oom_over=4)
    state = _state(tmp_path, backend, max_batch_items=16)
    state.warmup()
    items = [{"text": f"第{index}句。", "voicePrompt": "青年男声"} for index in range(16)]

    content, durations, _ = state.synthesize_batch({"items": items})

    assert len(durations) == 16
    assert backend.batch_sizes[0] == 16  # 先按客户端要的整包试
    # 16 → 8 → 4 逐级减半；4 条成功之后立刻升回 16（所以紧接着是 12 条一包，而不是继续 4）
    assert backend.batch_sizes[:4] == [16, 8, 4, 12]
    assert state.inflight == 0
    with io.BytesIO(content) as buffer:
        with zipfile.ZipFile(buffer) as archive:
            assert len([name for name in archive.namelist() if name.endswith(".wav")]) == 16

    # 第二次请求仍从整包开始：自愈不留跨请求的降档记忆
    backend.batch_sizes.clear()
    state.synthesize_batch({"items": items})
    assert backend.batch_sizes[0] == 16


def test_single_item_oom_splits_by_sentence(tmp_path):
    backend = LengthOomBackend(max_len=12)
    state = _state(tmp_path, backend)
    state.warmup()
    text = "第一句话很长很长。第二句话也很长很长。第三句话同样很长。"

    _content, durations, _ = state.synthesize_batch(
        {"items": [{"text": text, "voicePrompt": "青年男声"}]}
    )

    assert len(durations) == 1
    assert backend.texts[0] == text  # 先整条试一次
    assert len(backend.texts) > 1  # 整条过不去 → 切分重试
    assert "".join(backend.ok_texts) == text  # 切分后一个字都没丢
    expected = sum(max(0.12, len(piece) * 0.06) for piece in backend.ok_texts)
    assert durations[0] == pytest.approx(expected, abs=0.02)


def test_persistent_oom_surfaces_service_error_oom(tmp_path):
    state = _state(tmp_path, DeadBackend())
    state.warmup()

    with pytest.raises(ServiceError) as info:
        state.synthesize({"text": "第一句。", "voicePrompt": "青年男声"})
    assert info.value.code == "oom"
    assert info.value.status_code == 503

    with pytest.raises(ServiceError) as batch_info:
        state.synthesize_batch(
            {"items": [{"text": f"第{index}句。", "voicePrompt": "青年男声"} for index in range(4)]}
        )
    assert batch_info.value.code == "oom"
    assert state.inflight == 0
