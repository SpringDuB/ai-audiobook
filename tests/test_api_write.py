from audiobook import jobs, store

from test_api_read import _client, _conn, _seed_book


def test_patch_line_writes_file_and_invalidates(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_write_bytes(store.chapter_render_meta_path(settings, book_id, 0), b"{}")
    response = client.patch(
        f"/api/books/{book_id}/lines/c0000-s01-l001", json={"text": "改过的第一句。", "emotion": "愤怒"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["line"]["text"] == "改过的第一句。"
    # 旁白原本不带情绪（intensity=0），人工加情绪时用中位数 0.5 兜底
    assert body["line"]["emotion"] == {"dominant": "愤怒", "intensity": 0.5, "source": "manual"}
    assert body["invalidated"] == {"chapter": 0, "render_meta_removed": True}
    assert store.read_jsonl(store.lines_path(settings, book_id, 0))[0]["text"] == "改过的第一句。"
    assert client.patch(f"/api/books/{book_id}/lines/nope", json={"text": "x"}).status_code == 404
    bad = client.patch(f"/api/books/{book_id}/lines/c0000-s01-l001", json={"nope": 1})
    assert bad.status_code == 400 and "不可修改的字段" in bad.json()["detail"]


def test_patch_line_keeps_speaker_when_name_is_sent_back(settings, narrator_lines):
    """界面上把当前说话人原样提交（角色名）时，不能把 speaker 字段写坏。"""
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {"characters": [{"id": "role_0001", "name": "张卫东", "gender": "男", "age_group": "青年"}]},
    )
    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    rows[0]["addressee"] = "role_0001"
    rows[0]["addressee_name"] = "张卫东"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), rows)
    body = client.patch(
        f"/api/books/{book_id}/lines/c0000-s01-l001",
        json={"text": "改一句。", "speaker": "旁白", "addressee": "张卫东"},
    ).json()
    assert body["line"]["speaker"] == "narrator"
    assert body["line"]["speaker_name"] == "旁白"
    assert body["line"]["addressee"] == "role_0001"
    assert body["line"]["addressee_name"] == "张卫东"


def test_resynth_enqueues_line_job(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    body = client.post(f"/api/books/{book_id}/lines/c0000-s01-l002/resynth").json()
    job = jobs.get_job(_conn(settings), body["job_id"])
    assert (body["chapter_index"], job.kind) == (0, "synthesize_line")
    assert job.progress == {"pending_line": "c0000-s01-l002"}


def test_export_endpoint_enqueues_book_export(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    body = client.post(f"/api/books/{book_id}/export", json={"mode": "chapter", "force": True}).json()
    assert jobs.get_job(_conn(settings), body["job_id"]).kind == "book_export"


def test_render_chapter_endpoint_invalidates_and_enqueues_post(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_write_bytes(store.chapter_render_meta_path(settings, book_id, 0), b"{}")
    store.atomic_write_bytes(store.book_wav_path(settings, book_id), b"RIFF")
    body = client.post(f"/api/books/{book_id}/chapters/0/render").json()
    job = jobs.get_job(_conn(settings), body["job_id"])
    assert (job.kind, job.chapter_index) == ("post", 0)
    assert not store.chapter_render_meta_path(settings, book_id, 0).exists()
    assert client.post("/api/books/nope/chapters/0/render").status_code == 404


def test_issues_retry_requeues_affected_chapters(settings, narrator_lines):
    from audiobook.analysis.issues import record_issue

    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    record_issue(settings, book_id, "tts_line_failed", reason="失败", chapter=0)
    record_issue(settings, book_id, "extract_text_drift", reason="别的类型", chapter=None)
    body = client.post(f"/api/books/{book_id}/issues/retry", json={"kinds": ["tts_line_failed"]}).json()
    assert body["chapters"] == [0]
    job = jobs.get_job(_conn(settings), body["job_ids"][0])
    assert (job.kind, job.chapter_index) == ("synthesize", 0)


def test_job_retry_and_cancel_endpoints(settings):
    client = _client(settings)
    conn = _conn(settings)
    job_id = jobs.enqueue(conn, "post", "b1", 1)
    while jobs.get_job(conn, job_id).status != "failed":
        jobs.claim(conn, "w1")
        jobs.fail(conn, job_id, "w1", "boom", retry_delay_ms=0)
    assert client.post(f"/api/jobs/{job_id}/retry").json()["ok"] is True
    assert jobs.get_job(conn, job_id).status == "queued"
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert jobs.get_job(conn, job_id).status == "canceled"
    assert client.post("/api/jobs/99999/retry").status_code == 404


def test_update_casting_writes_back_and_invalidates(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "narrator_voice": "default",
            "roles": {"narrator": {"role_id": "narrator", "name": "旁白", "voice_id": "default"}},
        },
    )
    store.atomic_write_bytes(store.chapter_render_meta_path(settings, book_id, 0), b"{}")
    body = client.put(f"/api/books/{book_id}/casting/narrator", json={"voice_id": "v001"}).json()
    assert body["role"]["voice_id"] == "v001"
    assert body["invalidated"] == [0]
    assert store.read_json(store.casting_path(settings, book_id))["roles"]["narrator"]["voice_id"] == "v001"
    assert client.put(f"/api/books/{book_id}/casting/nope", json={"voice_id": "x"}).status_code == 404


def test_update_settings_writes_overlay(settings):
    client = _client(settings)
    body = client.put("/api/settings", json={"loudness_mode": "rms", "pause_max_ms": 900}).json()
    assert body["settings"]["loudness_mode"] == "rms"
    assert body["overlay_keys"] == ["loudness_mode", "pause_max_ms"]
    assert client.get("/api/settings").json()["settings"]["pause_max_ms"] == 900
    rejected = client.put("/api/settings", json={"data_dir": "/etc"})
    assert rejected.status_code == 400 and "不可通过界面修改" in rejected.json()["detail"]
