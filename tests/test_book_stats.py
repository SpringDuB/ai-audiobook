from audiobook import jobs, store
from fake_engine import FakeEngine


def _synth_clips(settings, book_id, index, rows, ms_per_char=10.0):
    engine = FakeEngine(ms_per_char=ms_per_char)
    for row in rows:
        engine.synthesize(row["text"], "default", None, store.audio_dir(settings, book_id, index) / f"{row['id']}.wav")


def test_book_stats_reports_progress_and_duration(settings, narrator_lines):
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    assert store.book_stats(settings, "b1")["state"] == "empty"
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"),
        {"chapters": [{"index": 0, "title": "卷一", "content": "第一句。", "chars": 4}]},
    )
    # 分章好了、还没点「一键分析」：状态是待分析，不是分析中
    assert store.book_stats(settings, "b1")["state"] == "split"
    rows = narrator_lines(0, "第一句。第二句。")
    store.write_jsonl_atomic(store.lines_path(settings, "b1", 0), rows)
    _synth_clips(settings, "b1", 0, rows)
    store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", 0), b"RIFF")
    store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", 0), b"1\n")
    store.atomic_replace_json(
        store.chapter_render_meta_path(settings, "b1", 0),
        {"duration": 12.5, "cues": 2, "generated_at": "2026-09-24T13:00:00+08:00"},
    )
    stats = store.book_stats(settings, "b1")
    assert (stats["analyzed"], stats["generated"], stats["duration_sec"], stats["state"]) == (1, 1, 12.5, "ready")
    assert stats["issues"] == 0 and stats["chapters"] == 1

    detail = store.chapter_state(settings, "b1", 0)
    assert (detail["lines"], detail["segments"], detail["state"], detail["duration_sec"]) == (2, 2, "rendered", 12.5)
    assert detail["rendered_at"] == "2026-09-24T13:00:00+08:00"


def test_chapter_state_walks_through_phases(settings, narrator_lines):
    assert store.chapter_state(settings, "b2", 3)["state"] == "empty"
    rows = narrator_lines(3, "只有旁白。")
    store.write_jsonl_atomic(store.lines_path(settings, "b2", 3), rows)
    assert store.chapter_state(settings, "b2", 3)["state"] == "analyzed"
    _synth_clips(settings, "b2", 3, rows)
    assert store.chapter_state(settings, "b2", 3)["state"] == "synthesized"


def test_book_stats_says_analyzing_only_while_analysis_jobs_are_active(settings, conn):
    store.atomic_replace_json(
        store.chapters_path(settings, "b3"),
        {"chapters": [{"index": 0, "title": "卷一", "content": "第一句。", "chars": 4}]},
    )
    assert store.book_stats(settings, "b3", conn)["state"] == "split"

    jobs.enqueue(conn, "characters", "b3")
    assert store.book_stats(settings, "b3", conn)["state"] == "analyzing"

    conn.execute("UPDATE jobs SET status='done' WHERE book_id='b3'")
    assert store.book_stats(settings, "b3", conn)["state"] == "split"
    jobs.enqueue(conn, "synthesize", "b3", 0)
    assert store.book_stats(settings, "b3", conn)["state"] == "split"   # 合成任务不算"分析中"


def test_settings_overlay_path_and_issue_count(settings):
    from audiobook.analysis.issues import record_issue

    assert store.settings_overlay_path(settings) == settings.data_dir / "settings.json"
    record_issue(settings, "b1", "audio_missing", reason="缺片段")
    assert store.count_issues(settings, "b1") == 1
    assert store.count_issues(settings, "b-none") == 0


# --- 书架/章节列表的热路径缓存（/api/books 从 2.5s 降到 50ms 靠的就是这几个）---


def test_chapter_state_cache_tracks_new_clips_and_line_rewrites(settings, narrator_lines):
    rows = narrator_lines(0, "第一句。第二句。")
    store.write_jsonl_atomic(store.lines_path(settings, "b7", 0), rows)
    assert store.chapter_state(settings, "b7", 0)["state"] == "analyzed"
    # 同一份文件再问一次：命中缓存，但结果必须还是对的
    assert store.chapter_state(settings, "b7", 0) == store.chapter_state(settings, "b7", 0)

    _synth_clips(settings, "b7", 0, rows)  # 片段目录 mtime 变了 → 缓存必须失效
    detail = store.chapter_state(settings, "b7", 0)
    assert (detail["segments"], detail["state"]) == (2, "synthesized")

    store.write_jsonl_atomic(store.lines_path(settings, "b7", 0), narrator_lines(0, "只有一句。"))
    assert store.chapter_state(settings, "b7", 0)["lines"] == 1


def test_chapter_state_does_not_hand_out_the_cached_dict(settings, narrator_lines):
    store.write_jsonl_atomic(store.lines_path(settings, "b4", 0), narrator_lines(0, "第一句。"))
    first = store.chapter_state(settings, "b4", 0)
    first["state"] = "tampered"
    first["lines"] = 999
    assert store.chapter_state(settings, "b4", 0)["state"] == "analyzed"
    assert store.chapter_state(settings, "b4", 0)["lines"] == 1


def test_chapter_index_drops_text_and_follows_rewrites(settings):
    store.atomic_replace_json(
        store.chapters_path(settings, "b5"),
        {"chapters": [{"index": 0, "title": "卷一", "content": "正文" * 500, "chars": 4}]},
    )
    assert store.chapter_index(settings, "b5") == [{"index": 0, "title": "卷一", "chars": 4}]
    # 全文只在看原文时才要，章节列表/书架统计不该把它拖着走
    assert "content" not in store.chapter_index(settings, "b5")[0]
    assert store.chapter_list(settings, "b5")[0]["content"] == "正文" * 500

    store.atomic_replace_json(
        store.chapters_path(settings, "b5"),
        {
            "chapters": [
                {"index": 0, "title": "卷一", "content": "x", "chars": 1},
                {"index": 1, "title": "卷二", "content": "y", "chars": 1},
            ]
        },
    )
    assert [item["index"] for item in store.chapter_index(settings, "b5")] == [0, 1]
