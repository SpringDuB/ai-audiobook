import pytest

from audiobook import store
from audiobook.analysis.issues import ISSUE_KINDS, record_issue


def test_record_issue_appends_machine_readable_row(settings):
    row = record_issue(
        settings,
        "book1",
        "extract_window_failed",
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
        "chapter_extract_failed",
        "extract_window_failed",
        "extract_text_drift",
        "unknown_speaker",
        "emotion_missing",
        "role_merge_failed",
        "role_merge_incomplete",
        "voice_library_empty",
        "voice_recommend_failed",
        "voice_recommend_invalid",
        "cast_sheet_failed",
        "voice_design_failed",
        "voice_design_incomplete",
        "tts_line_failed",
        "tts_ref_missing",
        "tts_endpoint_down",
        "audio_missing",
        "render_duration_mismatch",
        "book_export_gap",
    )


def test_every_record_issue_kind_is_registered():
    """回归：`voice_design_failed` 曾经漏登记，导致降级路径直接抛 ValueError 盖住真实错误。"""
    import re
    from pathlib import Path

    used: set[str] = set()
    root = Path(__file__).resolve().parents[1] / "src" / "audiobook"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"kind\s*=\s*[\"']([a-z_]+)[\"']", text))
        used.update(re.findall(r"[\"']kind[\"']\s*:\s*[\"']([a-z_]+)[\"']", text))
    # 行内 kind（dialogue/narration）和 job kind 不是 issue kind，不在校验范围
    ignored = {"dialogue", "narration", "chapter_split"}
    missing = sorted(used - set(ISSUE_KINDS) - ignored)
    assert missing == [], f"这些 kind 没登记进 ISSUE_KINDS: {missing}"


def test_book_layout_paths(settings):
    assert store.characters_path(settings, "b1").as_posix().endswith("books/b1/analysis/characters.json")
    assert store.casting_path(settings, "b1").as_posix().endswith("books/b1/voices/casting.json")
    assert store.voice_path(settings, "v_x").as_posix().endswith("voices/v_x/voice.json")
    assert store.pronounce_path(settings).as_posix().endswith("data/pronounce.json")
