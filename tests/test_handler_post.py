import pytest

from audiobook import audio, jobs, store
from fake_engine import FakeEngine
from audiobook.handlers import post, synthesize  # noqa: F401  导入即注册
from audiobook.worker import WorkerContext, run_once


def _run_pipeline(conn, settings, narrator_lines, text="第一句。第二句。", chapter=1):
    engine = FakeEngine(ms_per_char=10.0)
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    store.write_jsonl_atomic(store.lines_path(settings, "b1", chapter), narrator_lines(chapter, text))
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", chapter)
    while run_once(ctx):
        pass
    return ctx


def test_post_writes_chapter_wav_and_srt_with_pauses(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    wav = store.output_dir(settings, "b1") / "chapter_0001.wav"
    srt = store.output_dir(settings, "b1") / "chapter_0001.srt"
    assert wav.exists() and srt.exists()
    # 每句 1102 帧（0.05s），每段后 6615 帧静音（300ms）=> 15434/22050 ≈ 0.69995s
    assert audio.wav_duration(wav) == pytest.approx(0.70, abs=1e-3)
    text = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,050" in text
    assert "00:00:00,350 --> 00:00:00,400" in text


def test_post_skips_missing_clip_and_records_issue(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").unlink()
    jobs.enqueue(conn, "post", "b1", 1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert any(row["kind"] == "audio_missing" and row["line"] == "c0001-s01-l002" for row in issues)
    assert audio.wav_duration(store.output_dir(settings, "b1") / "chapter_0001.wav") > 0


def test_post_records_render_metadata(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    meta = store.read_json(store.chapter_render_meta_path(settings, "b1", 1))
    assert (meta["cues"], meta["clips"]) == (2, 2)
    assert meta["duration"] == pytest.approx(0.70, abs=1e-2)


def test_post_is_idempotent_when_nothing_changed(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    wav = store.output_dir(settings, "b1") / "chapter_0001.wav"
    stamp = wav.stat().st_mtime_ns
    jobs.enqueue(conn, "post", "b1", 1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    assert wav.stat().st_mtime_ns == stamp          # 没变输入 → 不重编码
