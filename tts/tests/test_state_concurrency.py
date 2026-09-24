import threading
import time

import pytest

from _stub_backend import StubBackend
from aiab_tts.config import TtsSettings
from aiab_tts.state import ServiceError, ServiceState, gpu_info


class SlowBackend(StubBackend):
    def __init__(self, delay: float = 0.2):
        super().__init__(delay=delay)
        self.max_inflight = 0
        self._active = 0
        self._lock = threading.Lock()

    def synthesize(self, request):
        with self._lock:
            self._active += 1
            self.max_inflight = max(self.max_inflight, self._active)
        try:
            return super().synthesize(request)
        finally:
            with self._lock:
                self._active -= 1


def _state(tmp_path, backend=None, **overrides) -> ServiceState:
    settings = TtsSettings(data_dir=tmp_path / "data", **overrides)
    return ServiceState(backend or StubBackend(), settings)


def _ref(state: ServiceState) -> str:
    return state.add_ref(b"RIFFfake", "参考")["refId"]


def test_health_reports_configured_capacity_and_gpu_fields(tmp_path):
    state = _state(tmp_path, max_concurrency=3)
    state.warmup()
    health = state.health()
    assert health["recommendedConcurrency"] == 3
    assert health["modelLoaded"] is True
    assert "vramTotalMB" in health and "vramUsedMB" in health
    assert set(gpu_info(TtsSettings())) >= {"device", "vramTotalMB", "vramUsedMB"}


def test_concurrency_gate_serializes_when_capacity_is_one(tmp_path):
    backend = SlowBackend(delay=0.2)
    state = _state(tmp_path, backend, max_concurrency=1)
    state.warmup()
    ref_id = _ref(state)

    def run() -> None:
        state.synthesize({"text": "第一句。", "refId": ref_id, "lang": "ZH"})

    started = time.monotonic()
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert time.monotonic() - started >= 0.35
    assert backend.max_inflight == 1


def test_oom_is_mapped_to_service_error(tmp_path):
    state = _state(tmp_path, StubBackend(oom_on={"爆炸"}), max_concurrency=1)
    state.warmup()
    ref_id = _ref(state)
    with pytest.raises(ServiceError) as excinfo:
        state.synthesize({"text": "会爆炸的句子", "refId": ref_id})
    assert excinfo.value.code == "oom"
    assert excinfo.value.status_code == 503


def test_unload_then_synthesize_reloads_automatically(tmp_path):
    state = _state(tmp_path, max_concurrency=2)
    state.warmup()
    ref_id = _ref(state)
    state.unload()
    assert state.health()["status"] == "unloaded"
    assert state.health()["recommendedConcurrency"] == state.capacity

    result = state.synthesize({"text": "第一句。", "refId": ref_id})
    assert result.duration_sec > 0
    assert state.health()["modelLoaded"] is True


def test_average_inference_ratio_is_tracked(tmp_path):
    state = _state(tmp_path, SlowBackend(delay=0.05), max_concurrency=1)
    state.warmup()
    ref_id = _ref(state)
    state.synthesize({"text": "第一句。", "refId": ref_id})
    assert state.health()["avgInferenceSecPerAudioSec"] > 0


def test_busy_when_queue_timeout_is_exceeded(tmp_path):
    state = _state(tmp_path, SlowBackend(delay=0.4), max_concurrency=1, queue_timeout_seconds=0.05)
    state.warmup()
    ref_id = _ref(state)
    started = threading.Event()

    def hold() -> None:
        started.set()
        state.synthesize({"text": "第一句。", "refId": ref_id})

    holder = threading.Thread(target=hold)
    holder.start()
    assert started.wait(timeout=1.0)
    time.sleep(0.05)
    with pytest.raises(ServiceError) as excinfo:
        state.synthesize({"text": "第二句。", "refId": ref_id})
    assert excinfo.value.code == "busy"
    holder.join()
