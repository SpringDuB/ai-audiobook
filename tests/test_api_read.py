from fastapi.testclient import TestClient

from audiobook import store
from audiobook.api.app import create_app
from audiobook.db import connect, init_db


def _conn(settings):
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


def _client(settings):
    return TestClient(create_app(settings, _conn(settings)))


def _seed_book(settings, narrator_lines, book_id="b1"):
    conn = _conn(settings)
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (book_id, "测试书", "source/original.txt", 1, "imported", 1790000000000),
    )
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "测试书"})
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": 0, "title": "卷一", "content": "第一句。第二句。", "chars": 8}]},
    )
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), narrator_lines(0, "第一句。第二句。"))
    store.atomic_replace_json(store.casting_path(settings, book_id), {"narrator_voice": "default", "roles": {}})
    return book_id


def test_shelf_reports_stats(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    payload = client.get("/api/books").json()
    row = next(item for item in payload["books"] if item["id"] == book_id)
    assert row["title"] == "测试书"
    assert (row["stats"]["chapters"], row["stats"]["analyzed"], row["stats"]["state"]) == (1, 1, "analyzed")


def test_chapters_endpoint_waits_instead_of_404_while_splitting(settings):
    """刚导入的书章节还没落盘：返回 200 + 空列表，别让书页变成一个假 404。"""
    client = _client(settings)
    conn = _conn(settings)
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        ("fresh", "新导入", "source/original.txt", 0, "imported", 1790000000000),
    )
    store.atomic_replace_json(store.book_dir(settings, "fresh") / "book.json", {"id": "fresh", "title": "新导入"})

    response = client.get("/api/books/fresh/chapters")
    assert response.status_code == 200
    assert response.json() == {"chapters": [], "status": "imported", "pending": True}


def test_chapters_endpoint_404s_only_for_unknown_books(settings):
    assert _client(settings).get("/api/books/没这本书/chapters").status_code == 404


def test_chapters_endpoint_lists_state(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    rows = client.get(f"/api/books/{book_id}/chapters").json()["chapters"]
    assert [(r["index"], r["title"], r["lines"], r["state"]) for r in rows] == [(0, "卷一", 2, "analyzed")]
    assert client.get("/api/books/nope/chapters").status_code == 404


def test_lines_endpoint_404s_for_unknown_chapter(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    assert client.get(f"/api/books/{book_id}/chapters/9/lines").status_code == 404


def test_lines_endpoint_exposes_audio_url_and_voice_prompt(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    lines = client.get(f"/api/books/{book_id}/chapters/0/lines").json()["lines"]
    assert [line["id"] for line in lines] == ["c0000-s01-l001", "c0000-s01-l002"]
    assert lines[0]["speaker_name"] == "旁白"
    # 这个分支前端只认「本句表演描述」；emotion 是历史字段，留在 payload 里只为兼容老数据
    assert lines[0]["voice_prompt"] == ""
    assert lines[0]["emotion"]["dominant"] == "平静"      # 夹具不注入场景基调 → source=none
    assert lines[0]["has_audio"] is False and lines[0]["duration_sec"] == 0.0
    assert lines[0]["audio_mtime"] is None
    assert lines[0]["audio_url"] == f"/api/books/{book_id}/lines/c0000-s01-l001/audio"
    assert client.get(lines[0]["audio_url"]).status_code == 404


def test_line_audio_serves_wav_when_present(settings, narrator_lines):
    from fake_engine import FakeEngine

    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    clip = store.audio_dir(settings, book_id, 0) / "c0000-s01-l001.wav"
    FakeEngine(ms_per_char=10.0).synthesize("第一句。", "default", None, clip)
    lines = client.get(f"/api/books/{book_id}/chapters/0/lines").json()["lines"]
    assert lines[0]["has_audio"] is True and lines[0]["duration_sec"] > 0
    assert lines[0]["audio_mtime"] and lines[0]["audio_mtime"] > 0
    # 同一句重生成后路径不变，必须禁止浏览器拿旧缓存
    response = client.get(lines[0]["audio_url"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    response = client.get(lines[0]["audio_url"])
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"


def test_issues_endpoint_returns_newest_first(settings):
    from audiobook.analysis.issues import record_issue

    client = _client(settings)
    record_issue(settings, "b1", "extract_text_drift", reason="第一条")
    record_issue(settings, "b1", "audio_missing", reason="第二条", line="c0000-s01-l002")
    issues = client.get("/api/books/b1/issues").json()["issues"]
    assert [row["reason"] for row in issues] == ["第二条", "第一条"]


def test_voices_endpoint_lists_library(settings):
    client = _client(settings)
    assert client.get("/api/voices").json()["voices"] == []
    store.atomic_replace_json(
        store.voice_path(settings, "v001"),
        {"id": "v001", "name": "张卫东", "gender": "男", "age_group": "青年", "tags": ["沉稳"]},
    )
    store.atomic_write_bytes(settings.voices_dir / "v001" / "ref.wav", b"RIFFfake")
    voices = client.get("/api/voices").json()["voices"]
    assert [(v["id"], v["name"], v["has_ref"]) for v in voices] == [("v001", "张卫东", True)]
    assert client.get("/api/voices/v001/sample").status_code == 200
    assert client.get("/api/voices/nope/sample").status_code == 404


def test_casting_endpoint_returns_roles_and_voices(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "narrator_voice": "default",
            "roles": {"narrator": {"role_id": "narrator", "name": "旁白", "voice_id": "default", "score": 0.0}},
        },
    )
    payload = client.get(f"/api/books/{book_id}/casting").json()
    assert payload["narrator_voice"] == "default"
    assert payload["roles"][0]["role_id"] == "narrator"
    assert payload["voices"] == []


def test_settings_endpoint_hides_api_key(settings):
    client = _client(settings)
    payload = client.get("/api/settings").json()
    assert payload["settings"]["llm_api_key_set"] is True
    assert "llm_api_key" not in payload["settings"]
    assert payload["overlay_keys"] == []
    assert payload["settings"]["loudness_mode"] == "off"      # 测试夹具
    assert payload["settings"]["export_container"] == "mkv"
