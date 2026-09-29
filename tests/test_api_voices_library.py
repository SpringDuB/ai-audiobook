"""音色库写操作：上传新音色、停用/启用，以及停用后的可见性。"""

from fastapi.testclient import TestClient

from audiobook import store
from audiobook.analysis.casting import load_voice_library, voice_catalog_text
from audiobook.api.app import create_app
from audiobook.db import connect, init_db
from helpers import wav_bytes


def _client(settings):
    conn = connect(settings.db_path)
    init_db(conn)
    return TestClient(create_app(settings, conn))


def _upload(client, **overrides):
    payload = {
        "name": "测试上传音色",
        "gender": "男",
        "age_group": "青年",
        "speech_rate": "中",
        "usage_type": "角色对话",
        "tags": "磁性,低沉",
        "description": "低沉克制的男声，适合反派独白",
    }
    payload.update(overrides)
    return client.post(
        "/api/voices",
        files={"file": ("ref.wav", wav_bytes(3.0), "audio/wav")},
        data=payload,
    )


def test_upload_voice_adds_to_library(settings):
    client = _client(settings)
    response = _upload(client)
    assert response.status_code == 200, response.text
    voice = response.json()["voice"]
    assert voice["id"] == "u001" and voice["name"] == "测试上传音色"
    assert voice["tags"] == ["磁性", "低沉"] and voice["has_ref"] is True
    assert voice["disabled"] is False and voice["source"] == "upload"

    ref = settings.voices_dir / "u001" / "ref.wav"
    assert ref.exists() and store.read_json(settings.voices_dir / "u001" / "voice.json")["ref"]["duration"] > 2
    listed = client.get("/api/voices").json()["voices"]
    assert [item["id"] for item in listed] == ["u001"]

    # 大模型看到的是同一份：名字、标签、介绍都要进提示词
    voices = load_voice_library(settings)
    assert [item.id for item in voices] == ["u001"]
    catalog = voice_catalog_text(voices)
    assert "u001｜测试上传音色" in catalog and "磁性" in catalog and "反派独白" in catalog


def test_upload_rejects_bad_input(settings):
    client = _client(settings)
    assert _upload(client, name="  ").status_code == 400
    assert client.post(
        "/api/voices",
        files={"file": ("ref.txt", b"not audio", "text/plain")},
        data={"name": "x"},
    ).status_code == 400
    assert client.post(
        "/api/voices",
        files={"file": ("ref.wav", wav_bytes(0.2), "audio/wav")},
        data={"name": "太短"},
    ).status_code == 400
    assert client.get("/api/voices").json()["voices"] == []   # 失败的都没留下半个音色


def test_disable_hides_voice_from_library_picker_and_llm(settings):
    client = _client(settings)
    voice_id = _upload(client).json()["voice"]["id"]

    disabled = client.patch(f"/api/voices/{voice_id}", json={"disabled": True})
    assert disabled.status_code == 200 and disabled.json()["voice"]["disabled"] is True

    assert client.get("/api/voices").json()["voices"] == []          # 选音色入口看不到
    assert load_voice_library(settings) == []                        # 大模型看不到
    kept = client.get("/api/voices", params={"include_disabled": True}).json()["voices"]
    assert [(item["id"], item["disabled"]) for item in kept] == [(voice_id, True)]
    # 参考音频还留着，随时能启用
    assert (settings.voices_dir / voice_id / "ref.wav").exists()
    assert client.get(f"/api/voices/{voice_id}/sample").status_code == 200

    revived = client.patch(f"/api/voices/{voice_id}", json={"disabled": False})
    assert revived.json()["voice"]["disabled"] is False
    assert [item.id for item in load_voice_library(settings)] == [voice_id]


def test_patch_unknown_or_illegal_voice_id(settings):
    client = _client(settings)
    assert client.patch("/api/voices/nope", json={"disabled": True}).status_code == 404
    assert client.patch("/api/voices/..%2Fescape", json={"disabled": True}).status_code in (400, 404)


def test_casting_endpoint_skips_disabled_voices(settings, narrator_lines):
    store.atomic_replace_json(
        store.voice_path(settings, "v001"),
        {"id": "v001", "name": "可用", "disabled": False},
    )
    store.atomic_write_bytes(store.voice_path(settings, "v002").parent / "ref.wav", b"RIFFfake")
    store.atomic_replace_json(
        store.voice_path(settings, "v002"),
        {"id": "v002", "name": "停用的", "disabled": True},
    )
    client = _client(settings)
    assert [item["id"] for item in client.get("/api/voices").json()["voices"]] == ["v001"]
