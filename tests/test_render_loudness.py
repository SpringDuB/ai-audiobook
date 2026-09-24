import pytest

from audiobook.config import get_settings
from audiobook.render.ffmpeg import probe_wav
from audiobook.render.loudness import measure_loudness, measure_rms, normalize_to_file
from helpers import requires_ffmpeg, write_tone


@pytest.fixture()
def lufs(settings):
    return settings.model_copy(update={"loudness_mode": "lufs"})


@requires_ffmpeg
def test_measure_loudness_reads_json_block(settings, tmp_path):
    src = write_tone(tmp_path / "tone.wav", seconds=2.0, rate=24000, freq=220, amp=0.0316)
    data = measure_loudness(settings, src)
    assert -35.0 < float(data["input_i"]) < -32.0      # 实测 -33.95
    assert float(data["input_tp"]) < 0


@requires_ffmpeg
def test_measure_rms_reads_mean_and_max(settings, tmp_path):
    src = write_tone(tmp_path / "tone.wav", seconds=2.0, rate=24000, freq=220, amp=0.0316)
    mean, peak = measure_rms(settings, src)
    assert (mean, peak) == (pytest.approx(-33.0, abs=0.5), pytest.approx(-30.0, abs=0.5))


@requires_ffmpeg
def test_loudnorm_two_pass_hits_target_and_keeps_duration(lufs, tmp_path):
    src = write_tone(tmp_path / "tone.wav", seconds=2.0, rate=24000, freq=220, amp=0.0316)
    dst = tmp_path / "norm.wav"
    result = normalize_to_file(lufs, src, dst, sample_rate=24000)
    assert result.mode == "lufs" and result.target == -16.0
    info = probe_wav(dst)
    assert (info.sample_rate, info.channels) == (24000, 1)
    assert info.duration == pytest.approx(2.0, abs=0.02)
    after = measure_loudness(lufs, dst)
    assert float(after["input_i"]) == pytest.approx(-16.0, abs=0.6)
    assert float(after["input_tp"]) <= -1.4
    assert result.measured_before == pytest.approx(-33.95, abs=1.0)   # 与第一遍测量一致


@requires_ffmpeg
def test_rms_mode_hits_target_without_clipping(settings, tmp_path):
    rms = get_settings(data_dir=settings.data_dir, loudness_mode="rms", loudness_rms_target_db=-20.0)
    src = write_tone(tmp_path / "tone.wav", seconds=2.0, rate=24000, freq=220, amp=0.0316)
    dst = tmp_path / "rms.wav"
    result = normalize_to_file(rms, src, dst, sample_rate=24000)
    assert result.mode == "rms" and result.applied_gain_db == pytest.approx(13.0, abs=0.5)
    mean, peak = measure_rms(rms, dst)
    assert mean == pytest.approx(-20.0, abs=0.5)
    assert peak <= -1.4
    assert probe_wav(dst).duration == pytest.approx(2.0, abs=0.02)


def test_off_mode_is_pure_copy(settings, tmp_path):
    off = get_settings(data_dir=settings.data_dir, loudness_mode="off")
    src = write_tone(tmp_path / "tone.wav", seconds=0.5, rate=24000, freq=220)
    dst = tmp_path / "copy.wav"
    result = normalize_to_file(off, src, dst, sample_rate=24000)
    assert (result.mode, result.applied_gain_db, result.measured_before) == ("off", None, None)
    assert dst.read_bytes() == src.read_bytes()


@requires_ffmpeg
def test_true_peak_caps_gain_in_rms_mode(settings, tmp_path):
    """目标 RMS 很高时，增益必须被真峰值上限压住，不能削顶。"""
    loud = get_settings(data_dir=settings.data_dir, loudness_mode="rms", loudness_rms_target_db=-3.0)
    src = write_tone(tmp_path / "tone.wav", seconds=1.0, rate=24000, freq=220, amp=0.9)
    dst = tmp_path / "capped.wav"
    result = normalize_to_file(loud, src, dst, sample_rate=24000)
    assert result.applied_gain_db == pytest.approx(-1.5 - (-0.9), abs=0.5)   # 峰值顶到 -1.5 dBTP 为止
    _, peak = measure_rms(loud, dst)
    assert peak <= -1.4


@requires_ffmpeg
def test_normalize_resamples_output_to_requested_rate(lufs, tmp_path):
    src = write_tone(tmp_path / "tone.wav", seconds=1.0, rate=22050, freq=220, amp=0.0316)
    dst = tmp_path / "resampled.wav"
    normalize_to_file(lufs, src, dst, sample_rate=48000)
    info = probe_wav(dst)
    assert info.sample_rate == 48000
    assert info.duration == pytest.approx(1.0, abs=0.02)
