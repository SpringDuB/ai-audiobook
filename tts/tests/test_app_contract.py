import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from _stub_backend import StubBackend
from aiab_tts.app import create_app
from aiab_tts.state import ServiceState
from aiab_tts.config import TtsSettings


def _client(tmp_path, **overrides) -> TestClient:
    settings = TtsSettings(data_dir=tmp_path / "data", **overrides)
    return TestClient(create_app(settings, ServiceState(StubBackend(), settings)))


def _ref(client, text="第一句。", **extra) -> tuple[str, object]:
    ref = client.post(
        "/v1/refs",
        files={"file": ("ref.wav", b"RIFFfake", "audio/wav")},
        data={"refText": "参考文本"},
    ).json()
    payload = {"text": text, "refId": ref["refId"], "lang": "ZH", "rate": 1.0, **extra}
    return ref["refId"], client.post("/v1/synthesize", json=payload)


def test_capabilities_declare_emotion_text_support(tmp_path):
    payload = _client(tmp_path).get("/capabilities").json()
    assert payload["emotionText"] is True


def test_synthesize_forwards_emotion_text_and_prefers_it(tmp_path):
    """emoText 通道：服务端把这句话原样交给后端（文本描述优先于 8 维向量）。"""
    settings = TtsSettings(data_dir=tmp_path / "data")
    backend = StubBackend()
    client = TestClient(create_app(settings, ServiceState(backend, settings)))
    _ref_id, response = _ref(
        client,
        text="你给我住手！",
        emoText="压着火气，语速比平时快",
        emoVector=[0, 0.9, 0, 0, 0, 0, 0, 0],
    )
    assert response.status_code == 200
    request = backend.requests[-1]
    assert request.emotion_text == "压着火气，语速比平时快"
    assert request.emo_vector == (0, 0.9, 0, 0, 0, 0, 0, 0)


def test_synthesize_rejects_over_long_emotion_text(tmp_path):
    client = _client(tmp_path)
    ref = client.post("/v1/refs", files={"file": ("ref.wav", b"RIFFfake", "audio/wav")}).json()
    response = client.post(
        "/v1/synthesize",
        json={"text": "第一句。", "refId": ref["refId"], "emoText": "很长" * 80},
    )
    assert response.status_code == 400


def test_health_reports_self_declared_concurrency(tmp_path):
    payload = _client(tmp_path, max_concurrency=3).get("/health").json()
    assert payload["status"] == "unloaded"
    assert payload["engine"] == "stub-tts"
    assert payload["inflight"] == 0
    assert "modelLoaded" in payload and "modelSource" in payload

    client = _client(tmp_path, max_concurrency=3)
    client.post("/warmup")
    warmed = client.get("/health").json()
    assert warmed["recommendedConcurrency"] == 3
    assert warmed["modelLoaded"] is True


def test_debug_memory_falls_back_when_backend_has_no_report(tmp_path):
    """桩后端没有自检实现也不能 500，至少要给 loaded 状态。"""
    payload = _client(tmp_path).get("/debug/memory").json()
    assert payload["backend"] == "stub-tts"
    assert payload["loaded"] is False


def test_debug_memory_prefers_backend_report(tmp_path):
    settings = TtsSettings(data_dir=tmp_path / "data")
    backend = StubBackend()
    backend.memory_report = lambda: {
        "backend": "stub-tts",
        "loaded": False,
        "model": {"cpuResidentMB": 0.0, "modules": []},
    }
    client = TestClient(create_app(settings, ServiceState(backend, settings)))
    payload = client.get("/debug/memory").json()
    assert payload["model"]["cpuResidentMB"] == 0.0


@pytest.fixture()
def tuning_reset():
    from aiab_tts.indextts_compat import SPEED_TUNING

    before = dict(SPEED_TUNING)
    yield
    SPEED_TUNING.update(before)


def test_debug_tuning_roundtrip(tmp_path, tuning_reset):
    client = _client(tmp_path)
    assert client.get("/debug/tuning").json() == {"numBeams": 1, "diffusionSteps": 16, "cfgRate": 0.7}

    updated = client.post("/debug/tuning", json={"numBeams": 3, "diffusionSteps": 25}).json()
    assert updated["numBeams"] == 3 and updated["diffusionSteps"] == 25
    assert client.get("/debug/tuning").json()["diffusionSteps"] == 25

    assert client.post("/debug/tuning", json={"numBeams": 0}).status_code == 400
    assert client.post("/debug/tuning", json={"diffusionSteps": None}).json()["diffusionSteps"] == 16


def _upload_ref(client) -> str:
    return client.post(
        "/v1/refs", files={"file": ("ref.wav", b"RIFFfake", "audio/wav")}
    ).json()["refId"]


def test_synthesize_batch_falls_back_per_item_without_backend_support(tmp_path):
    """桩后端没有批量能力：接口照样要能用（逐条合成），返回 zip + manifest。"""
    client = _client(tmp_path)
    ref_id = _upload_ref(client)
    response = client.post(
        "/v1/synthesize_batch",
        json={
            "refId": ref_id,
            "lang": "ZH",
            "items": [{"text": "第一句。"}, {"text": "第二句。", "emoVector": [0] * 7 + [0.5]}],
        },
    )
    assert response.status_code == 200
    assert response.headers["x-item-count"] == "2"
    assert len(response.headers["x-item-durations"].split(",")) == 2
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist()[:2] == ["000.wav", "001.wav"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["count"] == 2
        assert manifest["batched"] is False
        assert len(manifest["durations"]) == 2
        assert all(value > 0 for value in manifest["durations"])


def test_synthesize_batch_uses_backend_batch_when_available(tmp_path):
    settings = TtsSettings(data_dir=tmp_path / "data")
    backend = StubBackend()
    calls: list[int] = []

    def fake_batch(requests, out_paths):
        calls.append(len(requests))
        durations = []
        for request, path in zip(requests, out_paths):
            result = backend.synthesize(request)
            path.write_bytes(result.audio)
            durations.append(result.duration_sec)
        return durations

    backend.synthesize_batch = fake_batch
    client = TestClient(create_app(settings, ServiceState(backend, settings)))
    ref_id = _upload_ref(client)
    response = client.post(
        "/v1/synthesize_batch",
        json={"refId": ref_id, "items": [{"text": "甲。"}, {"text": "乙。"}, {"text": "丙。"}]},
    )
    assert response.status_code == 200
    assert calls == [3]                     # 一次解码三条
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("manifest.json"))["batched"] is True


def test_synthesize_batch_rejects_empty_and_oversized(tmp_path):
    client = _client(tmp_path)
    ref_id = _upload_ref(client)
    assert client.post("/v1/synthesize_batch", json={"refId": ref_id, "items": []}).status_code == 400
    too_many = [{"text": f"第{i}句。"} for i in range(9)]     # 默认 AIAB_TTS_MAX_BATCH_ITEMS=8
    response = client.post("/v1/synthesize_batch", json={"refId": ref_id, "items": too_many})
    assert response.status_code == 400
    assert "最多" in response.json()["detail"]["message"]


def test_synthesize_batch_rejects_unknown_ref(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/v1/synthesize_batch", json={"refId": "ref_nope", "items": [{"text": "甲。"}]}
    )
    assert response.status_code == 404


def test_capabilities_match_frozen_contract(tmp_path):
    payload = _client(tmp_path).get("/capabilities").json()
    assert payload["emotions"] is True
    assert payload["emotionDims"] == [
        "happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm",
    ]
    assert payload["rate"] is True and payload["rateRange"] == [0.5, 2.0]
    assert payload["pronunciation"] is True
    assert payload["languages"] == ["ZH", "EN", "JP", "ES", "AR"]
    assert payload["sampleRate"] == 22050
    assert payload["maxTextChars"] == 300


def test_ref_upload_then_synthesize_returns_playable_wav(tmp_path):
    client = _client(tmp_path)
    ref = client.post(
        "/v1/refs",
        files={"file": ("ref.wav", b"RIFFfake", "audio/wav")},
        data={"refText": "参考文本"},
    )
    assert ref.status_code == 200
    ref_id = ref.json()["refId"]

    response = client.post(
        "/v1/synthesize",
        json={
            "text": "第一句。",
            "refId": ref_id,
            "lang": "ZH",
            "emoVector": [0, 0, 0, 0, 0, 0, 0, 0.5],
            "rate": 1.0,
            "pronunciation": {"重": "CHONG2"},
            "format": "wav",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert float(response.headers["X-Duration-Sec"]) > 0.05
    assert response.headers["X-Engine"] == "stub-tts"
    assert response.content[:4] == b"RIFF"


def test_synthesize_rejects_unknown_ref_and_empty_text(tmp_path):
    client = _client(tmp_path)
    missing = client.post("/v1/synthesize", json={"text": "第一句。", "refId": "nope", "lang": "ZH"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "bad_ref"

    ref_id = client.post("/v1/refs", files={"file": ("ref.wav", b"RIFFfake", "audio/wav")}).json()["refId"]
    empty = client.post("/v1/synthesize", json={"text": "   ", "refId": ref_id, "lang": "ZH"})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "bad_request"

    too_fast = client.post("/v1/synthesize", json={"text": "第一句。", "refId": ref_id, "rate": 3.0})
    assert too_fast.status_code == 400
    assert too_fast.json()["detail"]["code"] == "bad_request"


def test_warmup_and_unload_transition_status(tmp_path):
    client = _client(tmp_path)
    warm = client.post("/warmup").json()
    assert warm["ok"] is True and warm["modelLoaded"] is True
    unloaded = client.post("/unload").json()
    assert unloaded["ok"] is True and unloaded["modelLoaded"] is False
    assert client.get("/health").json()["status"] == "unloaded"
