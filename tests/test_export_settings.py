from pathlib import Path

from audiobook import store
from audiobook.config import get_settings


def test_export_settings_defaults(tmp_path):
    s = get_settings(data_dir=tmp_path / "data")
    # 默认 rms：章的响度照样对齐，但渲染只要 ~0.4s（lufs 两遍 loudnorm 要 ~43s/42分钟章）
    assert (s.loudness_mode, s.loudness_target_lufs, s.loudness_true_peak) == ("rms", -16.0, -1.5)
    assert (s.export_target_sample_rate, s.export_container, s.export_mkv) == (0, "mkv", True)
    assert (s.loudness_rms_target_db, s.ffmpeg_path) == (-20.0, "")
    assert s.ffmpeg_timeout_seconds == 1800.0


def test_export_settings_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AB_LOUDNESS_MODE", "rms")
    monkeypatch.setenv("AB_EXPORT_TARGET_SAMPLE_RATE", "48000")
    s = get_settings(data_dir=tmp_path / "data")
    assert (s.loudness_mode, s.export_target_sample_rate) == ("rms", 48000)


def test_artifact_paths_are_stable(tmp_path):
    s = get_settings(data_dir=tmp_path / "data")
    out = store.output_dir(s, "b1")
    assert store.chapter_wav_path(s, "b1", 7) == out / "chapter_0007.wav"
    assert store.chapter_srt_path(s, "b1", 7) == out / "chapter_0007.srt"
    assert store.chapter_media_path(s, "b1", 7, ".mkv") == out / "chapter_0007.mkv"
    assert store.chapter_media_path(s, "b1", 7, "mp4") == out / "chapter_0007.mp4"
    assert store.chapter_render_meta_path(s, "b1", 7) == out / "chapter_0007.render.json"
    assert store.render_work_dir(s, "b1", 7) == store.audio_dir(s, "b1", 7) / "_render"
    assert store.book_wav_path(s, "b1") == out / "book.wav"
    assert store.book_srt_path(s, "b1") == out / "book.srt"
    assert store.book_media_path(s, "b1", ".mkv") == out / "book.mkv"
    assert store.export_target_dir(s, "b1") == out
    assert store.export_target_dir(s, "b1", tmp_path / "merged") == Path(tmp_path / "merged")
