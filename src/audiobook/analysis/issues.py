import time

from .. import store

ISSUE_KINDS = (
    # 提取 / 整合 / 推荐
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
    # 合成 / 渲染
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
