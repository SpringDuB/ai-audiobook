from audiobook import audio, jobs, store
from audiobook.api.app import create_app  # noqa: F401  确保导入链路完整
from audiobook.db import connect, init_db
from audiobook.engines.fake import FakeEngine
from audiobook.handlers import post, split, synthesize  # noqa: F401
from audiobook.importer import import_book
from audiobook.worker import WorkerContext, run_once


def test_full_pipeline_without_gpu_produces_chapter_artifacts(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    txt = tmp_path / "地球最后一个修仙者.txt"
    # 有前言时：前言=index 0、第一章=index 1、第二章=index 2
    txt.write_text(
        "简介：一个修仙者的故事。\n\n第一章 重生十年前\n\n第一句。第二句。\n\n第二章 死党王胖子\n\n第三句。",
        encoding="utf-8",
    )
    book_id = import_book(settings, conn, txt, title="地球最后一个修仙者")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine(ms_per_char=10.0))

    guard = 0
    while run_once(ctx):
        guard += 1
        assert guard < 200, "任务链没有收敛"

    out = store.output_dir(settings, book_id)
    assert (out / "chapter_0001.wav").exists()
    assert (out / "chapter_0001.srt").exists()
    assert (out / "chapter_0002.wav").exists()
    assert audio.wav_duration(out / "chapter_0001.wav") > 0.1
    assert all(j.status == "done" for j in jobs.list_jobs(conn, book_id))


def test_rerun_after_change_only_regenerates_changed_line(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    txt = tmp_path / "b.txt"
    txt.write_text("简介：一本。\n\n第一章 重生十年前\n\n第一句。第二句。", encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    engine = FakeEngine(ms_per_char=10.0)
    calls = {"n": 0}
    original = engine.synthesize

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    engine.synthesize = counting  # type: ignore[method-assign]
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    while run_once(ctx):
        pass
    first = calls["n"]

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    rows[1]["text"] = "改过的第二句。"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 1), rows)
    jobs.enqueue(conn, "synthesize", book_id, 1)
    while run_once(ctx):
        pass
    assert calls["n"] == first + 1
