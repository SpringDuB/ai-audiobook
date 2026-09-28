import time

from .. import store

ISSUE_KINDS = (
    "pass_a_failed",
    "pass_a_chapter_skipped",
    "pass_c_failed",
    "chapter_analysis_failed",
    "chapter_window_failed",
    "line_index_missing",
    "unknown_speaker",
    "voice_library_empty",
    "casting_voice_reused",
    "casting_no_match",
    "tts_line_failed",
    "tts_ref_missing",
    "tts_endpoint_down",
    "audio_missing",
    "render_duration_mismatch",
    "book_export_gap",
)


def record_issue(
    settings,
    book_id: str,
    kind: str,
    *,
    reason: str,
    chapter: int | None = None,
    scene: str | None = None,
    line: str | None = None,
    fallback: str | None = None,
    detail: dict | None = None,
) -> dict:
    """把降级/失败写入 issues.jsonl —— 失败必须可见，不允许静默。"""
    if kind not in ISSUE_KINDS:
        raise ValueError(f"未知的异常类型: {kind}")
    row = {
        "ts": int(time.time() * 1000),
        "book_id": book_id,
        "kind": kind,
        "chapter": chapter,
        "scene": scene,
        "line": line,
        "reason": str(reason),
        "fallback": fallback,
        "detail": detail or {},
    }
    store.append_jsonl(store.issues_path(settings, book_id), row)
    return row
