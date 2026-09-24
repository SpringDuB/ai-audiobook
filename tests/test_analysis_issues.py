import pytest

from audiobook import store
from audiobook.analysis.issues import ISSUE_KINDS, record_issue


def test_record_issue_appends_machine_readable_row(settings):
    row = record_issue(
        settings,
        "book1",
        "pass_c_failed",
        reason="LlmJsonError: 2 次都失败",
        chapter=1,
        scene="c0001-s02",
        fallback="整场景按旁白 + 场景基调",
        detail={"sentences": 12},
    )

    rows = store.read_jsonl(store.issues_path(settings, "book1"))
    assert len(rows) == 1
    assert rows[0] == row
    assert set(row) == {"ts", "book_id", "kind", "chapter", "scene", "line", "reason", "fallback", "detail"}
    assert row["detail"] == {"sentences": 12}
    assert row["line"] is None


def test_record_issue_rejects_unknown_kind(settings):
    with pytest.raises(ValueError):
        record_issue(settings, "book1", "not_a_kind", reason="x")


def test_issue_kinds_are_frozen():
    assert ISSUE_KINDS == (
        "pass_a_failed",
        "pass_a_chapter_skipped",
        "pass_b_failed",
        "scene_hint_not_found",
        "pass_c_failed",
        "line_index_missing",
        "unknown_speaker",
        "voice_library_empty",
        "casting_voice_reused",
        "casting_no_match",
        "tts_line_failed",
        "tts_ref_missing",
        "tts_endpoint_down",
        "audio_missing",
    )


def test_book_layout_paths(settings):
    assert store.characters_path(settings, "b1").as_posix().endswith("books/b1/analysis/characters.json")
    assert store.scenes_path(settings, "b1", 7).as_posix().endswith("books/b1/analysis/scenes/chapter_0007.json")
    assert store.casting_path(settings, "b1").as_posix().endswith("books/b1/voices/casting.json")
    assert store.voice_path(settings, "v_x").as_posix().endswith("voices/v_x/voice.json")
    assert store.pronounce_path(settings).as_posix().endswith("data/pronounce.json")
