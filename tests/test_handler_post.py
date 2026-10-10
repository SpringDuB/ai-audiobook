from pathlib import Path

import pytest

from audiobook import audio, jobs, listen, store
from fake_engine import FakeEngine
from audiobook.handlers import post, synthesize  # noqa: F401  导入即注册
from audiobook.worker import WorkerContext, run_once
from helpers import requires_ffmpeg


def _run_pipeline(conn, settings, narrator_lines, text="第一句。第二句。", chapter=1):
    engine = FakeEngine(ms_per_char=10.0)
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    store.write_jsonl_atomic(store.lines_path(settings, "b1", chapter), narrator_lines(chapter, text))
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", chapter)
    while run_once(ctx):
        pass
    return ctx


def test_post_writes_chapter_wav_and_srt_without_gaps(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    wav = store.output_dir(settings, "b1") / "chapter_0001.wav"
    srt = store.output_dir(settings, "b1") / "chapter_0001.srt"
    assert wav.exists() and srt.exists()
    # 每句 1102 帧（0.05s），句间不插静音 => 2204/22050 ≈ 0.09995s
    assert audio.wav_duration(wav) == pytest.approx(0.10, abs=1e-3)
    text = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,050" in text
    assert "00:00:00,050 --> 00:00:00,100" in text


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
    assert meta["duration"] == pytest.approx(0.10, abs=1e-2)


def test_post_is_idempotent_when_nothing_changed(conn, settings, narrator_lines):
    _run_pipeline(conn, settings, narrator_lines)
    wav = store.output_dir(settings, "b1") / "chapter_0001.wav"
    stamp = wav.stat().st_mtime_ns
    jobs.enqueue(conn, "post", "b1", 1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    assert wav.stat().st_mtime_ns == stamp          # 没变输入 → 不重编码


def test_post_skips_mobile_prewarm_when_disabled(conn, settings, narrator_lines):
    """默认测试配置关掉预热：post 不该偷偷拉起 ffmpeg。"""
    _run_pipeline(conn, settings, narrator_lines)
    assert not listen.mobile_audio_path(settings, "b1", 1).exists()


@requires_ffmpeg
def test_post_prewarms_mobile_audio(conn, settings, narrator_lines):
    """章节产出后顺手把手机离线用的 m4a 转好：手机下载时就不用现场等转码。"""
    warm = settings.model_copy(update={"mobile_audio_prewarm": True})
    _run_pipeline(conn, warm, narrator_lines)
    path = listen.mobile_audio_path(warm, "b1", 1)
    assert path.exists() and path.stat().st_size > 0
    assert path.stat().st_mtime >= store.chapter_wav_path(warm, "b1", 1).stat().st_mtime
    assert Path(path).suffix == ".m4a"
