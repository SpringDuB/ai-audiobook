import pytest

from audiobook import jobs, store
from audiobook.engines.fake import FakeEngine
from audiobook.handlers import post, synthesize  # noqa: F401  导入即注册
from audiobook.render.book import export_book, parse_chapter_filter
from audiobook.render.ffmpeg import probe_json, probe_wav
from audiobook.render.srt import parse_srt
from audiobook.worker import WorkerContext, run_once
from helpers import requires_ffmpeg


def _produce(conn, settings, narrator_lines, chapters=(1, 2)):
    """用假引擎把指定章节跑成 output/chapter_*.wav + .srt（每章 0.70s）。"""
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "测试书"})
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"),
        {"chapters": [{"index": i, "title": "起风", "content": "第一句。"} for i in chapters]},
    )
    for index in chapters:
        store.write_jsonl_atomic(
            store.lines_path(settings, "b1", index), narrator_lines(index, "第一句。第二句。")
        )
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine(ms_per_char=10.0))
    for index in chapters:
        jobs.enqueue(conn, "synthesize", "b1", index)
    while run_once(ctx):
        pass


def test_parse_chapter_filter_supports_ranges_and_lists():
    assert parse_chapter_filter(None) is None
    assert parse_chapter_filter("") is None
    assert parse_chapter_filter("1,2,30-32") == {1, 2, 30, 31, 32}
    assert parse_chapter_filter("1..3") == {1, 2, 3}
    with pytest.raises(ValueError):
        parse_chapter_filter("abc")


def test_export_dry_run_lists_outputs_without_writing(conn, settings, narrator_lines):
    _produce(conn, settings, narrator_lines)
    report = export_book(settings, "b1", mode="all", dry_run=True)
    assert report.dry_run is True
    assert report.chapters == (1, 2)
    assert report.total_seconds == 0.0
    assert not store.book_wav_path(settings, "b1").exists()
    assert store.chapter_wav_path(settings, "b1", 1).exists()   # 原产物没动


@requires_ffmpeg
def test_export_all_writes_book_wav_srt_mkv_and_reports(conn, settings, narrator_lines):
    prod = settings.model_copy(update={"export_mkv": True})
    _produce(conn, prod, narrator_lines)
    report = export_book(prod, "b1", mode="all", force=True)
    assert report.chapters == (1, 2) and report.missing == ()
    assert probe_wav(store.book_wav_path(prod, "b1")).duration == pytest.approx(1.40, abs=0.05)
    cues = parse_srt(store.book_srt_path(prod, "b1"))
    assert [round(cue.start, 3) for cue in cues] == [0.0, 0.35, 0.7, 1.05]
    assert [cue.text for cue in cues] == ["第一句。", "第二句。", "第一句。", "第二句。"]
    assert report.cues == 4 and report.total_seconds == pytest.approx(1.40, abs=0.05)
    data = probe_json(prod, store.book_media_path(prod, "b1", ".mkv"))
    assert [stream["codec_type"] for stream in data["streams"]] == ["audio", "subtitle"]
    assert [chapter["tags"]["title"] for chapter in data["chapters"]] == ["第1章 起风", "第2章 起风"]
    assert (report.out_dir / "playlist.m3u").read_text(encoding="utf-8").count("chapter_") == 2
    assert "第1章 起风" in (report.out_dir / "book_章节.txt").read_text(encoding="utf-8")
    assert "book.wav" in (report.out_dir / "merge-report.txt").read_text(encoding="utf-8")


@requires_ffmpeg
def test_export_chapter_mode_skips_book_merge(conn, settings, narrator_lines):
    prod = settings.model_copy(update={"export_mkv": True})
    _produce(conn, prod, narrator_lines)
    report = export_book(prod, "b1", mode="chapter")
    assert not store.book_wav_path(prod, "b1").exists()
    assert (report.out_dir / "chapter_0001.mkv").exists()
    assert (report.out_dir / "chapter_0002.mkv").exists()


@requires_ffmpeg
def test_export_reports_missing_chapters(conn, settings, narrator_lines):
    _produce(conn, settings, narrator_lines, chapters=(1, 3))
    report = export_book(settings, "b1", mode="book", chapters={1, 2, 3})
    assert report.chapters == (1, 3)
    assert report.missing == (2,)
    assert any("第 2 章" in warning for warning in report.warnings)


@requires_ffmpeg
def test_export_to_custom_dir_keeps_canonical_outputs(conn, settings, narrator_lines, tmp_path):
    prod = settings.model_copy(update={"export_mkv": True})
    _produce(conn, prod, narrator_lines)
    target = tmp_path / "merged"
    report = export_book(prod, "b1", mode="all", out_dir=target)
    assert report.out_dir == target
    assert (target / "book.wav").exists() and (target / "chapter_0001.mkv").exists()
    assert store.chapter_wav_path(prod, "b1", 1).exists()      # 规范产物仍在 output/


@requires_ffmpeg
def test_export_containers_are_idempotent(conn, settings, narrator_lines):
    prod = settings.model_copy(update={"export_mkv": True})
    _produce(conn, prod, narrator_lines)
    export_book(prod, "b1", mode="chapter", force=True)
    dst = store.chapter_media_path(prod, "b1", 1, ".mkv")
    first = dst.stat().st_mtime_ns
    export_book(prod, "b1", mode="chapter")
    assert dst.stat().st_mtime_ns == first


def test_export_with_no_ready_chapter_warns(conn, settings):
    report = export_book(settings, "b1", mode="all")
    assert report.chapters == () and report.outputs == ()
    assert any("没有任何已完成章节" in warning for warning in report.warnings)
