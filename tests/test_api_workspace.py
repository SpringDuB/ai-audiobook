"""工作台用到的接口：原文、行号兜底、选角章节统计、一键启动 TTS。"""

import audiobook.api.app as app_module
from audiobook import store
from audiobook.config import load_overlay

from test_api_read import _client, _seed_book


def test_chapter_text_endpoint_returns_raw_content(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    body = client.get(f"/api/books/{book_id}/chapters/0/text").json()
    assert body == {"index": 0, "title": "卷一", "chars": 8, "content": "第一句。第二句。"}
    assert client.get(f"/api/books/{book_id}/chapters/9/text").status_code == 404


def test_lines_payload_fills_seq_for_legacy_rows(settings, narrator_lines):
    """老数据没有 seq / scene_index，界面上不能显示 null。"""
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    for row in rows:
        row.pop("seq", None)
        row.pop("scene_index", None)
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), rows)
    lines = client.get(f"/api/books/{book_id}/chapters/0/lines").json()["lines"]
    assert [line["seq"] for line in lines] == [1, 2]
    assert [line["scene_index"] for line in lines] == [1, 1]


def test_casting_reports_role_chapter_occurrences(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "测试书"})
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {"characters": [{"id": "role_0001", "name": "张卫东"}, {"id": "role_0002", "name": "秦风"}]},
    )
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "roles": {
                "narrator": {"role_id": "narrator", "name": "旁白", "voice_id": "v1", "voice_name": "音色一"},
                "role_0002": {"role_id": "role_0002", "name": "秦风", "voice_id": "v2", "voice_name": "音色二"},
            }
        },
    )
    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    rows[0]["addressee"] = "role_0002"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), rows)

    payload = client.get(f"/api/books/{book_id}/casting").json()
    roles = {role["role_id"]: role for role in payload["roles"]}
    assert roles["narrator"]["lines"] == 2 and roles["narrator"]["chapters"] == [0]
    assert roles["role_0002"]["lines"] == 0 and roles["role_0002"]["chapters"] == [0]
    assert payload["role_ids"] == ["narrator", "role_0002"]      # 按句数排序


def test_casting_put_can_create_role_known_from_characters(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {"characters": [{"id": "role_0001", "name": "张卫东"}]},
    )
    store.atomic_replace_json(store.casting_path(settings, book_id), {"roles": {}})
    response = client.put(f"/api/books/{book_id}/casting/role_0001", json={"voice_id": "v007", "voice_name": "浑厚男声"})
    assert response.status_code == 200
    assert response.json()["role"]["voice_name"] == "浑厚男声"
    assert response.json()["role"]["source"] == "manual"
    assert client.put(f"/api/books/{book_id}/casting/role_9999", json={"voice_id": "v001"}).status_code == 404


def test_tts_local_endpoints_switch_engine(settings, monkeypatch):
    class FakeService:
        def __init__(self, settings):  # noqa: ARG002 - 与真实类签名一致
            self.started = None

        def status(self):
            return {
                "running": bool(self.started),
                "healthy": bool(self.started),
                "starting": False,
                "pid": 4242 if self.started else None,
                "port": 8020,
                "url": "http://127.0.0.1:8020",
                "backend": "fake",
                "model_source": "local",
                "started_at": None,
                "health": None,
                "log_path": "data/logs/tts-service.log",
            }

        def start(self, **kwargs):
            self.started = kwargs
            port = kwargs.get("port") or 8020
            return {**self.status(), "port": port, "url": f"http://127.0.0.1:{port}"}

        def stop(self):
            self.started = None
            return self.status()

        def logs(self, *, offset=0, limit=300):  # noqa: ARG002
            return {"offset": 12, "lines": ["fake tts up"], "reset": offset == 0}

    monkeypatch.setattr(app_module, "LocalTtsService", FakeService)
    client = _client(settings)

    status = client.get("/api/tts/local").json()
    assert status["service"]["running"] is False
    assert status["launch"]["backend"] == settings.tts_backend

    started = client.post("/api/tts/local/start", json={"backend": "indextts", "port": 8123, "model_source": "local"}).json()
    assert started["engine"] == "http"
    assert started["endpoints"] == ["http://127.0.0.1:8123"]
    overlay = load_overlay(settings)
    assert overlay["engine"] == "http"
    assert overlay["tts_endpoints"] == ["http://127.0.0.1:8123"]
    assert overlay["tts_port"] == 8123
    assert overlay["tts_backend"] == "indextts"

    assert client.get("/api/tts/local/logs?offset=0").json()["lines"] == ["fake tts up"]
    assert client.post("/api/tts/local/stop").json()["service"]["running"] is False


def test_start_falls_back_to_indextts_when_stored_backend_is_gone(settings, monkeypatch):
    """老 data/settings.json 里存着 fake：一键启动要能自己纠正，而不是报"未知后端"。"""
    from audiobook.config import save_overlay

    save_overlay(settings, {"tts_backend": "fake"})
    seen = {}

    class FakeService:
        def __init__(self, settings):  # noqa: ARG002
            pass

        def status(self):
            return {"running": False, "healthy": False, "starting": False, "pid": None, "port": 8020,
                    "url": "http://127.0.0.1:8020", "backend": "indextts", "log_path": "x.log"}

        def start(self, **kwargs):
            seen.update(kwargs)
            return {**self.status(), "running": True, "port": kwargs.get("port") or 8020}

        def stop(self):
            return self.status()

        def logs(self, *, offset=0, limit=300):  # noqa: ARG002
            return {"offset": 0, "lines": [], "reset": True}

    monkeypatch.setattr(app_module, "LocalTtsService", FakeService)
    client = _client(settings)
    client.post("/api/tts/local/start", json={})
    assert seen["backend"] == "indextts"
