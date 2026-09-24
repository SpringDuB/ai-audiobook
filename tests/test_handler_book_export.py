from audiobook import jobs, store
from fake_engine import FakeEngine
from audiobook.handlers import book_export, post, synthesize  # noqa: F401  导入即注册
from audiobook.render.srt import parse_srt
from audiobook.worker import WorkerContext, run_once


def _produce(conn, settings, narrator_lines, chapters=(1, 2)):
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "测试书"})
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"),
        {"chapters": [{"index": i, "title": "起风", "content": "第一句。"} for i in chapters]},
    )
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine(ms_per_char=10.0))
    for index in chapters:
        store.write_jsonl_atomic(store.lines_path(settings, "b1", index), narrator_lines(index, "第一句。第二句。"))
        jobs.enqueue(conn, "synthesize", "b1", index)
    while run_once(ctx):
        pass
    return ctx


def test_book_export_job_writes_book_artifacts(conn, settings, narrator_lines):
    ctx = _produce(conn, settings, narrator_lines)
    jobs.enqueue(conn, "book_export", "b1")
    while run_once(ctx):
        pass
    row = conn.execute("SELECT status, error FROM jobs WHERE kind='book_export'").fetchone()
    assert (row["status"], row["error"]) == ("done", None)
    assert store.book_wav_path(settings, "b1").exists()
    cues = parse_srt(store.book_srt_path(settings, "b1"))
    assert [cue.text for cue in cues] == ["第一句。", "第二句。", "第一句。", "第二句。"]
    assert [round(cue.start, 3) for cue in cues] == [0.0, 0.35, 0.7, 1.05]
    assert (store.output_dir(settings, "b1") / "book_章节.txt").exists()
    assert (store.output_dir(settings, "b1") / "merge-report.txt").exists()


def test_book_export_job_finishes_when_no_chapter_ready(conn, settings):
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    jobs.enqueue(conn, "book_export", "b1")
    while run_once(ctx):
        pass
    row = conn.execute("SELECT status, progress FROM jobs WHERE kind='book_export'").fetchone()
    assert row["status"] == "done"          # 无章节属于"无可导出"，不制造失败任务
    assert not store.book_wav_path(settings, "b1").exists()
