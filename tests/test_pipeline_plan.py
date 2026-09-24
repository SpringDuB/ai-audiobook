from audiobook import store
from audiobook.pipeline import plan_book, resume_book


def _chapters(settings, book_id: str, indexes=(1, 2)) -> None:
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": i, "title": f"第{i}章", "content": "第一句。", "chars": 4} for i in indexes]},
    )


def _scenes(settings, book_id: str, index: int) -> None:
    store.atomic_replace_json(
        store.scenes_path(settings, book_id, index),
        {"book_id": book_id, "chapter_index": index, "scenes": []},
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


def test_plan_requests_scenes_per_chapter(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    assert plan_book(settings, conn, "b1") == [("scenes", 1), ("scenes", 2)]
    _scenes(settings, "b1", 1)
    # 已有场景的第 1 章转入 lines，缺场景的第 2 章继续排 scenes
    assert plan_book(settings, conn, "b1") == [("lines", 1), ("scenes", 2)]


def test_plan_requests_lines_for_chapters_with_scenes(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    _scenes(settings, "b1", 1)
    _scenes(settings, "b1", 2)
    assert plan_book(settings, conn, "b1") == [("lines", 1), ("lines", 2)]


def test_plan_requests_casting_then_missing_audio(settings, conn):
    _chapters(settings, "b1")
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    for index in (1, 2):
        _scenes(settings, "b1", index)
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
        _scenes(settings, "b1", index)
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
        _scenes(settings, "b1", index)
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
        _scenes(settings, "b1", index)
        _lines(settings, "b1", index)
        store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", index), b"RIFF")
        store.atomic_write_bytes(store.chapter_srt_path(settings, "b1", index), b"1\n")
    assert plan_book(settings, conn, "b1") == [("post", 1), ("post", 2)]
    _render_meta(settings, 1)
    assert plan_book(settings, conn, "b1") == [("post", 2)]


def test_resume_book_enqueues_the_planned_jobs(settings, conn):
    _chapters(settings, "b1")
    plan = resume_book(settings, conn, "b1")
    assert plan == [("characters", None)]
    row = conn.execute("SELECT kind, status FROM jobs WHERE book_id='b1'").fetchone()
    assert (row["kind"], row["status"]) == ("characters", "queued")


def test_phase_analysis_only_queues_the_analysis_chain(settings, conn):
    """「分析角色文本」按钮：只推分章→角色→场景→逐句→选角。"""
    assert resume_book(settings, conn, "b1", phase="analysis") == [("chapter_split", None)]
    _chapters(settings, "b1")
    assert resume_book(settings, conn, "b1", phase="analysis") == [("characters", None)]
    store.atomic_replace_json(store.characters_path(settings, "b1"), {"characters": []})
    plan = resume_book(settings, conn, "b1", phase="analysis")
    assert plan == [("scenes", 1), ("scenes", 2)]
    for index in (1, 2):
        _scenes(settings, "b1", index)
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
        _scenes(settings, "b1", index)
        _lines(settings, "b1", index)
    assert resume_book(settings, conn, "b1", phase="audio") == [("synthesize", 1), ("synthesize", 2)]
