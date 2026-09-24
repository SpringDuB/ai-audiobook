from fastapi.testclient import TestClient

from _stub_backend import StubBackend
from aiab_tts.app import create_app
from aiab_tts.state import ServiceState
from aiab_tts.config import TtsSettings


def _client(tmp_path, **overrides) -> TestClient:
    settings = TtsSettings(data_dir=tmp_path / "data", **overrides)
    return TestClient(create_app(settings, ServiceState(StubBackend(), settings)))


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
