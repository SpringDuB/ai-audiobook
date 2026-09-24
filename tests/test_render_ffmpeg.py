import os
from pathlib import Path

import pytest

from audiobook.engines.fake import FakeEngine
from audiobook.render.ffmpeg import (
    AudioInfo,
    FFmpegError,
    bundled_ffmpeg,
    find_ffmpeg,
    find_ffprobe,
    parse_ffmpeg_info,
    probe_json,
    probe_wav,
    run_ffmpeg,
)
from helpers import requires_ffmpeg


def test_find_ffmpeg_reports_missing_explicit_path():
    with pytest.raises(FFmpegError, match="不存在"):
        find_ffmpeg(r"D:\definitely\not\here\ffmpeg.exe")


def test_find_ffmpeg_uses_project_bundled_build():
    """项目自带 ffmpeg（imageio-ffmpeg 依赖）——用户不需要装 PATH。"""
    bundled = bundled_ffmpeg()
    assert bundled is not None, "imageio-ffmpeg 依赖应该自带 ffmpeg"
    assert Path(bundled).is_file()
    found = Path(find_ffmpeg())
    assert found.is_file() and found.name.lower().startswith("ffmpeg")


def test_find_ffprobe_prefers_ffmpeg_sibling(tmp_path):
    exe = ".exe" if os.name == "nt" else ""
    ffmpeg = tmp_path / f"ffmpeg{exe}"
    ffprobe = tmp_path / f"ffprobe{exe}"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    assert Path(find_ffprobe(str(ffmpeg))) == ffprobe


def test_find_ffprobe_is_optional():
    """没有 ffprobe 也要能跑：probe_json 会用 `ffmpeg -i` 兜底。"""
    probe = find_ffprobe()
    assert probe is None or Path(probe).is_file()


FFMPEG_INFO_SAMPLE = """
Input #0, matroska,webm, from 'book.mkv':
  Metadata:
    title           : 测试书
  Duration: 00:17.230, start: 0.000000, bitrate: 320 kb/s
  Chapters:
    Chapter #0:0: start 0.000000, end 17.230000
      Metadata:
        title           : 第一章 起风
  Stream #0:0: Audio: pcm_s16le, 22050 Hz, mono, s16, 352 kb/s
  Stream #0:1(chi): Subtitle: subrip (srt) (default)
"""


def test_parse_ffmpeg_info_reads_streams_and_chapters():
    data = parse_ffmpeg_info(FFMPEG_INFO_SAMPLE)
    assert data["format"]["format_name"] == "matroska,webm"
    assert data["format"]["duration"] == "17.230"
    assert [stream["codec_type"] for stream in data["streams"]] == ["audio", "subtitle"]
    assert data["streams"][0]["channels"] == 1 and data["streams"][0]["sample_rate"] == 22050
    assert data["streams"][1]["tags"]["language"] == "chi"
    assert [chapter["tags"]["title"] for chapter in data["chapters"]] == ["第一章 起风"]


@requires_ffmpeg
def test_probe_json_falls_back_to_ffmpeg(settings, tmp_path, monkeypatch):
    import audiobook.render.ffmpeg as ffmpeg_module

    monkeypatch.setattr(ffmpeg_module, "find_ffprobe", lambda explicit="": None)
    src = FakeEngine(sample_rate=24000, ms_per_char=10.0).synthesize("一二三", "v1", None, tmp_path / "a.wav").path
    data = probe_json(settings, src)
    assert data["streams"][0]["codec_type"] == "audio"
    assert data["streams"][0]["codec_name"] == "pcm_s16le"


def test_probe_wav_reads_header(tmp_path):
    result = FakeEngine(sample_rate=24000, ms_per_char=10.0).synthesize("一二三四五", "v1", None, tmp_path / "a.wav")
    info = probe_wav(result.path)
    assert info == AudioInfo(
        sample_rate=24000, channels=1, bits=16, duration=pytest.approx(0.05, abs=1e-3), frames=1200
    )


@requires_ffmpeg
def test_run_ffmpeg_failure_carries_stderr(settings, tmp_path):
    with pytest.raises(FFmpegError) as excinfo:
        run_ffmpeg(settings, ["-i", str(tmp_path / "missing.wav"), str(tmp_path / "out.wav")])
    assert "ffmpeg 退出码" in str(excinfo.value)
    assert "missing.wav" in str(excinfo.value)


@requires_ffmpeg
def test_run_ffmpeg_converts_sample_rate(settings, tmp_path):
    src = FakeEngine(sample_rate=22050, ms_per_char=10.0).synthesize("一二三四五", "v1", None, tmp_path / "a.wav").path
    dst = tmp_path / "b.wav"
    run_ffmpeg(settings, ["-i", str(src), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    assert probe_wav(dst).sample_rate == 24000


@requires_ffmpeg
def test_probe_json_lists_streams(settings, tmp_path):
    src = FakeEngine(sample_rate=24000, ms_per_char=10.0).synthesize("一二三", "v1", None, tmp_path / "a.wav").path
    data = probe_json(settings, src)
    assert data["streams"][0]["codec_type"] == "audio"
