from pathlib import Path

import pytest

from audiobook.engines.fake import FakeEngine
from audiobook.render.ffmpeg import (
    AudioInfo,
    FFmpegError,
    find_ffmpeg,
    find_ffprobe,
    probe_json,
    probe_wav,
    run_ffmpeg,
)
from helpers import requires_ffmpeg


def test_find_ffmpeg_reports_missing_explicit_path():
    with pytest.raises(FFmpegError, match="不存在"):
        find_ffmpeg(r"D:\definitely\not\here\ffmpeg.exe")


@requires_ffmpeg
def test_find_ffmpeg_and_ffprobe_from_path():
    assert Path(find_ffmpeg()).name.lower().startswith("ffmpeg")
    assert "ffprobe" in Path(find_ffprobe()).name.lower()


@requires_ffmpeg
def test_find_ffprobe_uses_ffmpeg_sibling():
    sibling = find_ffprobe(find_ffmpeg())
    assert Path(sibling).parent == Path(find_ffmpeg()).parent


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
