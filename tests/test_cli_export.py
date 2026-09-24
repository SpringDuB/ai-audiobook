from audiobook import store
from audiobook.cli import main
from audiobook.config import get_settings
from helpers import requires_ffmpeg, write_tone


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AB_LOUDNESS_MODE", "off")
    monkeypatch.setenv("AB_EXPORT_MKV", "false")
    settings = get_settings()
    store.atomic_replace_json(store.chapters_path(settings, "b1"), {"chapters": [{"index": 1, "title": "起风"}]})
    write_tone(store.chapter_wav_path(settings, "b1", 1), seconds=0.5, rate=24000, freq=220)
    store.atomic_write_text(
        store.chapter_srt_path(settings, "b1", 1), "1\n00:00:00,000 --> 00:00:00,500\n甲\n"
    )
    return settings


def test_cli_export_dry_run_prints_plan(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    assert main(["export", "b1", "--mode", "chapter", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "chapter_0001.mkv" in out
    assert not (store.output_dir(get_settings(), "b1") / "chapter_0001.mkv").exists()


def test_cli_export_rejects_bad_chapter_filter(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    assert main(["export", "b1", "--chapters", "abc"]) == 2
    assert "参数错误" in capsys.readouterr().out


@requires_ffmpeg
def test_cli_export_writes_book_files(tmp_path, monkeypatch, capsys):
    settings = _seed(tmp_path, monkeypatch)
    assert main(["export", "b1", "--mode", "book", "--no-container"]) == 0
    out = capsys.readouterr().out
    assert "book.wav" in out
    assert store.book_wav_path(settings, "b1").exists()
    assert store.book_srt_path(settings, "b1").exists()
    assert not store.book_media_path(settings, "b1", ".mkv").exists()


@requires_ffmpeg
def test_cli_export_reports_ffmpeg_failure(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("AB_EXPORT_MKV", "true")
    monkeypatch.setenv("AB_FFMPEG_PATH", str(tmp_path / "nope" / "ffmpeg.exe"))
    assert main(["export", "b1", "--mode", "chapter"]) == 1
    assert "导出失败" in capsys.readouterr().out
