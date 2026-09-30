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


def _seed_three_chapters(settings, narrator_lines) -> str:
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {
            "chapters": [
                {"index": index, "title": f"第{index}章", "content": "第一句。", "chars": 4}
                for index in (0, 1, 2)
            ]
        },
    )
    return book_id


def test_analyze_chapters_queues_one_batch_job_for_picked_chapters(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    store.atomic_replace_json(store.extract_path(settings, book_id, 0), {"windows": 1, "lines": []})
    store.atomic_replace_json(store.extract_path(settings, book_id, 1), {"windows": 1, "lines": []})
    body = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [1, 2]}).json()
    # 勾选多章合成一个批量任务（内部按大模型并发同时提这几章），不再一章一个 job
    assert body["plan"] == [["chapters", None]]
    # 默认断点续跑：旧提取结果保留（handler 会跳过已分析好的章，不再重复花 LLM）
    assert store.extract_path(settings, book_id, 1).exists()
    # 没勾的章一个文件都不碰
    assert store.extract_path(settings, book_id, 0).exists()
    job = jobs.get_job(_conn(settings), body["job_ids"][0])
    assert (job.kind, job.chapter_index) == ("chapters", None)
    assert job.payload == {"chapters": [1, 2]}


def test_analyze_chapters_force_keeps_old_extracts_until_rerun(settings, narrator_lines):
    """弹窗勾「覆盖重跑」→ force=true：不预删旧结果，轮到该章时才覆盖（中途取消不丢数据）。"""
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    store.atomic_replace_json(store.extract_path(settings, book_id, 1), {"windows": 1, "lines": []})
    body = client.post(
        f"/api/books/{book_id}/analyze/chapters", json={"chapters": [1], "force": True}
    ).json()
    assert body["plan"] == [["chapters", None]]
    assert store.extract_path(settings, book_id, 1).exists()
    job = jobs.get_job(_conn(settings), body["job_ids"][0])
    assert job.payload == {"chapters": [1], "force": True}


def test_analyze_all_chapters_force_passes_flag_to_characters_job(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    store.atomic_replace_json(store.extract_path(settings, book_id, 0), {"windows": 1, "lines": []})
    body = client.post(
        f"/api/books/{book_id}/analyze/chapters", json={"chapters": [0, 1, 2], "force": True}
    ).json()
    assert body["plan"] == [["characters", None]]
    assert store.extract_path(settings, book_id, 0).exists()
    assert jobs.get_job(_conn(settings), body["job_ids"][0]).payload == {"force": True}


def test_analyze_chapters_merges_second_click_into_queued_batch(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    first = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [1]}).json()
    second = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [2]}).json()
    # 还没开跑：两次勾选并进同一条批量任务，不会各跑一批
    assert second["job_ids"] == first["job_ids"]
    assert jobs.get_job(_conn(settings), first["job_ids"][0]).payload == {"chapters": [1, 2]}


def test_analyze_chapters_while_batch_running_queues_extra_chapters_as_lines(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    first = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [1]}).json()
    conn = _conn(settings)
    claimed = jobs.claim(conn, "w1")  # 模拟 worker 已经在跑这条批量任务
    assert claimed.id == first["job_ids"][0]

    body = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [1, 2]}).json()
    # 在跑的批量任务不认新清单：没覆盖的第 2 章退回按章 lines，已覆盖的第 1 章不重复排
    assert body["plan"] == [["lines", 2]]
    assert body["running_job_id"] == claimed.id
    job = jobs.get_job(conn, body["job_ids"][0])
    assert (job.kind, job.chapter_index) == ("lines", 2)


def test_analyze_chapters_with_all_selected_runs_full_book_merge(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    body = client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [0, 1, 2]}).json()
    assert body["plan"] == [["characters", None]]


def test_analyze_chapters_rejects_empty_or_unknown_selection(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_three_chapters(settings, narrator_lines)
    assert client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": []}).status_code == 400
    assert client.post(f"/api/books/{book_id}/analyze/chapters", json={"chapters": [9]}).status_code == 400


def test_generate_chapter_enqueues_casting_then_synthesis(settings, narrator_lines):
    """角色还没选音色：先补一轮选角，再合成这一章。"""
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    body = client.post(f"/api/books/{book_id}/chapters/0/generate").json()
    assert body["plan"] == [["casting", None], ["synthesize", 0]]
    kinds = [jobs.get_job(_conn(settings), job_id).kind for job_id in body["job_ids"]]
    assert kinds == ["casting", "synthesize"]


def test_generate_chapter_skips_casting_when_voices_are_bound(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {"roles": {"narrator": {"role_id": "narrator", "voice_id": "v001"}}},
    )
    body = client.post(f"/api/books/{book_id}/chapters/0/generate").json()
    assert body["plan"] == [["synthesize", 0]]


def test_generate_chapter_needs_lines(settings, narrator_lines):
    client = _client(settings)
    book_id = _seed_book(settings, narrator_lines)
    store.lines_path(settings, book_id, 0).unlink()
    response = client.post(f"/api/books/{book_id}/chapters/0/generate")
    assert response.status_code == 409 and "还没有分析结果" in response.json()["detail"]


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
    body = client.put("/api/settings", json={"loudness_mode": "rms", "export_container": "mp4"}).json()
    assert body["settings"]["loudness_mode"] == "rms"
    assert body["overlay_keys"] == ["export_container", "loudness_mode"]
    assert client.get("/api/settings").json()["settings"]["export_container"] == "mp4"
    rejected = client.put("/api/settings", json={"data_dir": "/etc"})
    assert rejected.status_code == 400 and "不可通过界面修改" in rejected.json()["detail"]
