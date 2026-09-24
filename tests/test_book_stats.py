from audiobook import store
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
    assert store.book_stats(settings, "b1")["state"] == "analyzing"
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


def test_settings_overlay_path_and_issue_count(settings):
    from audiobook.analysis.issues import record_issue

    assert store.settings_overlay_path(settings) == settings.data_dir / "settings.json"
    record_issue(settings, "b1", "audio_missing", reason="缺片段")
    assert store.count_issues(settings, "b1") == 1
    assert store.count_issues(settings, "b-none") == 0
