import json

import pytest

from audiobook import store
from audiobook.engines.fake import FakeEngine
from audiobook.render.chapter import build_clips, render_chapter, render_key
from audiobook.render.ffmpeg import probe_json, probe_wav
from conftest import make_narrator_lines
from helpers import requires_ffmpeg


def _seed_lines(settings, book_id="b1", chapter=1, text="第一句。第二句。"):
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "测试书"})
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": chapter, "title": f"第{chapter}章 起风", "content": text}]},
    )
    rows = make_narrator_lines(chapter, text)
    store.write_jsonl_atomic(store.lines_path(settings, book_id, chapter), rows)
    engine = FakeEngine(ms_per_char=10.0)
    for row in rows:
        engine.synthesize(
            row["text"], "default", None, store.audio_dir(settings, book_id, chapter) / f"{row['id']}.wav"
        )
    return rows


def test_build_clips_pairs_lines_with_audio_and_pauses(settings):
    _seed_lines(settings)
    clips, skipped = build_clips(settings, "b1", 1)
    assert skipped == []
    assert [clip.line_id for clip in clips] == ["c0001-s01-l001", "c0001-s01-l002"]
    assert [clip.pause_ms for clip in clips] == [300, 300]
    assert all(clip.duration == pytest.approx(0.05, abs=1e-3) for clip in clips)


def test_build_clips_reports_missing_audio(settings):
    _seed_lines(settings)
    (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").unlink()
    clips, skipped = build_clips(settings, "b1", 1)
    assert skipped == ["c0001-s01-l002"]
    assert len(clips) == 1


def test_render_chapter_off_mode_writes_wav_srt_and_meta(settings):
    _seed_lines(settings)
    result = render_chapter(settings, "b1", 1)
    assert result.cached is False
    assert (result.sample_rate, result.cues, result.clips) == (22050, 2, 2)
    assert result.container is None
    assert probe_wav(result.wav).duration == pytest.approx(0.70, abs=1e-2)
    text = result.srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,050" in text
    assert "00:00:00,350 --> 00:00:00,400" in text
    meta = json.loads(store.chapter_render_meta_path(settings, "b1", 1).read_text(encoding="utf-8"))
    assert meta["render_key"].startswith("sha256:")
    assert (meta["cues"], meta["sample_rate"], meta["loudness"]["mode"]) == (2, 22050, "off")
    assert meta["clips"] == 2 and meta["skipped"] == [] and meta["warnings"] == []


def test_render_chapter_is_idempotent_until_key_changes(settings):
    _seed_lines(settings)
    first = render_chapter(settings, "b1", 1)
    stamp = first.wav.stat().st_mtime_ns
    assert render_chapter(settings, "b1", 1).cached is True
    assert first.wav.stat().st_mtime_ns == stamp
    meta_path = store.chapter_render_meta_path(settings, "b1", 1)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    store.atomic_replace_json(meta_path, {**meta, "render_key": "sha256:stale"})
    third = render_chapter(settings, "b1", 1)
    assert third.cached is False and third.wav.stat().st_mtime_ns != stamp


def test_render_chapter_force_rerenders(settings):
    _seed_lines(settings)
    first = render_chapter(settings, "b1", 1)
    stamp = first.wav.stat().st_mtime_ns
    again = render_chapter(settings, "b1", 1, force=True)
    assert again.cached is False and again.wav.stat().st_mtime_ns != stamp


def test_render_chapter_pause_tail_extends_audio(settings):
    _seed_lines(settings)
    longer = settings.model_copy(update={"pause_tail_ms": 500})
    result = render_chapter(longer, "b1", 1)
    assert result.duration == pytest.approx(1.20, abs=0.02)   # 0.70 + 0.5


def test_render_key_changes_with_pause_settings(settings):
    _seed_lines(settings)
    clips, _ = build_clips(settings, "b1", 1)
    base = render_key(settings, clips)
    assert render_key(settings, clips) == base
    assert render_key(settings.model_copy(update={"pause_scale": 1.5}), clips) != base
    assert render_key(settings.model_copy(update={"loudness_mode": "lufs"}), clips) != base


@requires_ffmpeg
def test_render_chapter_with_loudness_and_container(settings):
    prod = settings.model_copy(update={"loudness_mode": "lufs", "export_mkv": True})
    _seed_lines(prod)
    result = render_chapter(prod, "b1", 1)
    assert result.container is not None and result.container.suffix == ".mkv"
    data = probe_json(prod, result.container)
    assert [stream["codec_type"] for stream in data["streams"]] == ["audio", "subtitle"]
    info = probe_wav(result.wav)
    assert (info.sample_rate, info.channels) == (22050, 1)
    assert info.duration == pytest.approx(0.70, abs=0.05)
    assert result.loudness is not None and result.loudness.mode == "lufs"


@requires_ffmpeg
def test_render_chapter_resamples_mixed_sources(settings):
    rows = _seed_lines(settings)
    FakeEngine(sample_rate=24000, ms_per_char=10.0).synthesize(
        rows[1]["text"], "default", None, store.audio_dir(settings, "b1", 1) / f"{rows[1]['id']}.wav"
    )
    result = render_chapter(settings, "b1", 1)
    assert result.sample_rate == 24000
    assert result.warnings == ()
    assert probe_wav(result.wav).duration == pytest.approx(0.70, abs=0.02)
