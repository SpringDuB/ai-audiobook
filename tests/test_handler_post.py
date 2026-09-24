import pytest

from audiobook import audio, jobs, store
from audiobook.engines.fake import FakeEngine
from audiobook.handlers import post, synthesize  # noqa: F401  导入即注册
from audiobook.text import lines_stub
from audiobook.worker import WorkerContext, run_once


def _run_pipeline(conn, settings, text="第一句。第二句。", chapter=1):
    engine = FakeEngine(ms_per_char=10.0)
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    store.write_jsonl_atomic(store.lines_path(settings, "b1", chapter), lines_stub.stub_lines(chapter, text))
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", chapter)
    while run_once(ctx):
        pass
    return ctx


def test_post_writes_chapter_wav_and_srt_with_pauses(conn, settings):
    _run_pipeline(conn, settings)
    wav = store.output_dir(settings, "b1") / "chapter_0001.wav"
    srt = store.output_dir(settings, "b1") / "chapter_0001.srt"
    assert wav.exists() and srt.exists()
    # 每句 1102 帧（0.05s），每段后 6615 帧静音（300ms）=> 15434/22050 ≈ 0.69995s
    assert audio.wav_duration(wav) == pytest.approx(0.70, abs=1e-3)
    text = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,050" in text
    assert "00:00:00,350 --> 00:00:00,400" in text


def test_post_skips_missing_clip_and_records_issue(conn, settings):
    _run_pipeline(conn, settings)
    (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").unlink()
    jobs.enqueue(conn, "post", "b1", 1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert any(row["id"] == "c0001-s01-l002" for row in issues)
    assert audio.wav_duration(store.output_dir(settings, "b1") / "chapter_0001.wav") > 0
