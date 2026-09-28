from audiobook import store
from audiobook.pipeline import plan_book, resume_book


def _chapters(settings, book_id: str, indexes=(1, 2)) -> None:
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": i, "title": f"第{i}章", "content": "第一句。", "chars": 4} for i in indexes]},
    )


def _lines(settings, book_id: str, index: int) -> None:
    store.write_jsonl_atomic(store.lines_path(settings, book_id, index), [{"id": "x", "text": "第一句。"}])


def _render_meta(settings, index: int) -> None:
    store.atomic_replace_json(
        store.chapter_render_meta_path(settings, "b1", index),
        {"render_key": f"sha256:{index}", "duration": 1.0, "cues": 1, "clips": 1, "sample_rate": 22050},
    )


def test_plan_starts_with_chapter_split(settings, conn):
    assert plan_book(settings, conn, "b1") == [("chapter_split", None)]


def test_plan_requests_characters_after_split(settings, conn):
    _chapters(settings, "b1")
    assert plan_book(settings, conn, "b1") == [("characters", None)]


def test_plan_requests_lines_per_chapter_after_characters(settings, conn):
    """没有场景切分了：角色分析之后直接逐句情感标注。"""
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    assert plan_book(settings, conn, "b1") == [("lines", 1), ("lines", 2)]
    _lines(settings, "b1", 1)
    assert plan_book(settings, conn, "b1") == [("lines", 2)]


def test_plan_requests_casting_then_missing_audio(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    for index in (1, 2):
        _lines(settings, "b1", index)
    assert plan_book(settings, conn, "b1") == [("casting", None)]

    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    assert plan_book(settings, conn, "b1") == [("synthesize", 1), ("synthesize", 2)]

    store.atomic_write_bytes(store.output_dir(settings, "b1") / "chapter_0001.wav", b"RIFF")
    assert plan_book(settings, conn, "b1") == [("synthesize", 2)]


def test_plan_is_empty_when_everything_exists(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    for index in (1, 2):
        _lines(settings, "b1", index)
        store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", index), b"RIFF")
        store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", index), b"1\n")
        _render_meta(settings, index)
    store.atomic_write_bytes(store.book_wav_path(settings, "b1"), b"RIFF")
    assert plan_book(settings, conn, "b1") == []


def test_plan_requests_book_export_after_all_chapters(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    for index in (1, 2):
        _lines(settings, "b1", index)
        store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", index), b"RIFF")
        store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", index), b"1\n")
        _render_meta(settings, index)
    assert plan_book(settings, conn, "b1") == [("book_export", None)]
    store.atomic_write_bytes(store.book_wav_path(settings, "b1"), b"RIFF")
    assert plan_book(settings, conn, "b1") == []


def test_plan_rerenders_chapters_without_render_meta(settings, conn):
    """M2 时代产出的章节（有 wav 没 render.json）要按 M3 设置补渲染。"""
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    for index in (1, 2):
        _lines(settings, "b1", index)
        store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", index), b"RIFF")
        store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", index), b"1\n")
    assert plan_book(settings, conn, "b1") == [("post", 1), ("post", 2)]
    _render_meta(settings, 1)
    assert plan_book(settings, conn, "b1") == [("post", 2)]


def test_plan_resynthesizes_chapters_after_a_voice_change(settings, conn):
    """换过音色：章节成品要重合成（只重跑缓存键变了的行），不能拿旧片段重渲染。"""
    _chapters(settings, "b1", indexes=(1,))
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    store.write_jsonl_atomic(
        store.lines_path(settings, "b1", 1),
        [{"id": "c0001-s01-l001", "speaker": "role_0001", "text": "第一句。"}],
    )
    store.atomic_replace_json(
        store.casting_path(settings, "b1"),
        {"roles": {"role_0001": {"role_id": "role_0001", "voice_id": "v_new"}}},
    )
    store.atomic_replace_json(
        store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.meta.json",
        {"voice_id": "v_old", "cache_key": "x", "duration": 1.0},
    )
    store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", 1), b"RIFF")
    store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", 1), b"1\n")
    _render_meta(settings, 1)
    assert plan_book(settings, conn, "b1") == [("synthesize", 1)]

    # 已经按新音色合成过 → 不再重复入队，正常收尾合本
    store.atomic_replace_json(
        store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.meta.json",
        {"voice_id": "v_new", "cache_key": "y", "duration": 1.0},
    )
    assert plan_book(settings, conn, "b1") == [("book_export", None)]


def test_resume_book_enqueues_the_planned_jobs(settings, conn):
    _chapters(settings, "b1")
    plan = resume_book(settings, conn, "b1")
    assert plan == [("characters", None)]
    row = conn.execute("SELECT kind, status FROM jobs WHERE book_id='b1'").fetchone()
    assert (row["kind"], row["status"]) == ("characters", "queued")


def test_phase_analysis_only_queues_the_analysis_chain(settings, conn):
    """「分析角色文本」按钮：只推分章 → 角色 → 逐句情感 → 选角。"""
    assert resume_book(settings, conn, "b1", phase="analysis") == [("chapter_split", None)]
    _chapters(settings, "b1")
    assert resume_book(settings, conn, "b1", phase="analysis") == [("characters", None)]
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    plan = resume_book(settings, conn, "b1", phase="analysis")
    assert plan == [("lines", 1), ("lines", 2)]
    for index in (1, 2):
        _lines(settings, "b1", index)
    assert resume_book(settings, conn, "b1", phase="analysis") == [("casting", None)]
    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    # 分析链跑完了：合成链的任务不该被这个按钮带出来
    assert resume_book(settings, conn, "b1", phase="analysis") == []


def test_phase_audio_only_queues_the_synthesis_chain(settings, conn):
    """「生成有声书」按钮：分析没好时什么都不推，分析好了只推合成/渲染/合本。"""
    _chapters(settings, "b1")
    assert resume_book(settings, conn, "b1", phase="audio") == []
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    store.atomic_replace_json(store.casting_path(settings, "b1"), {"roles": {}})
    for index in (1, 2):
        _lines(settings, "b1", index)
    assert resume_book(settings, conn, "b1", phase="audio") == [("synthesize", 1), ("synthesize", 2)]
