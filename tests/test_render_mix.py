import wave

import pytest

from audiobook import audio
from audiobook.render.mix import Clip, clip_from_wav, concat_clips, ensure_uniform, plan_target_rate
from helpers import estimate_freq, requires_ffmpeg, write_tone


def test_clip_from_wav_reads_header(tmp_path):
    path = write_tone(tmp_path / "a.wav", seconds=0.5, rate=24000, freq=220)
    clip = clip_from_wav("c0001-s01-l001", path, pause_ms=300, text="一。")
    assert (clip.sample_rate, clip.channels, clip.bits, clip.pause_ms) == (24000, 1, 16, 300)
    assert clip.duration == pytest.approx(0.5, abs=1e-4)


def test_plan_target_rate_prefers_setting_then_max(settings, tmp_path):
    clips = [
        Clip("a", tmp_path / "a.wav", 0, "", 1.0, 22050, 1, 16),
        Clip("b", tmp_path / "b.wav", 0, "", 1.0, 24000, 1, 16),
    ]
    assert plan_target_rate(settings, clips) == 24000
    assert plan_target_rate(settings.model_copy(update={"export_target_sample_rate": 48000}), clips) == 48000


def test_naive_frame_concat_of_mixed_rates_is_corrupt(tmp_path):
    """证明 spec 里记录的静默损坏：帧数直接相加 + 采头采样率 = 时长与变速都错。"""
    fast = write_tone(tmp_path / "fast.wav", seconds=1.0, rate=24000, freq=220)
    slow = write_tone(tmp_path / "slow.wav", seconds=1.0, rate=22050, freq=220)
    with pytest.raises(audio.FormatMismatch):
        audio.concat_with_pauses([(fast, 0), (slow, 0)], tmp_path / "naive.wav")
    with wave.open(str(tmp_path / "naive.wav"), "wb") as out:   # 手写"天真拼接"
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        for path in (fast, slow):
            with wave.open(str(path)) as src:
                out.writeframes(src.readframes(src.getnframes()))
    with wave.open(str(tmp_path / "naive.wav")) as handle:
        assert handle.getnframes() == 46050                        # 24000 + 22050
        assert handle.getnframes() / 24000 == pytest.approx(1.91875, abs=1e-5)
        # 22050Hz 的 220Hz 被当成 24000Hz 播 → 239Hz（变速变调）
        assert estimate_freq(tmp_path / "naive.wav", 1.1, 1.9) == pytest.approx(239.0, rel=0.02)


@requires_ffmpeg
def test_ensure_uniform_resamples_and_keeps_duration(settings, tmp_path):
    fast = clip_from_wav("a", write_tone(tmp_path / "fast.wav", seconds=1.0, rate=24000, freq=220))
    slow = clip_from_wav("b", write_tone(tmp_path / "slow.wav", seconds=1.0, rate=22050, freq=220))
    clips, target = ensure_uniform(settings, [fast, slow], tmp_path / "work")
    assert target == 24000
    assert [clip.sample_rate for clip in clips] == [24000, 24000]
    out = tmp_path / "mixed.wav"
    assert concat_clips(clips, out) == pytest.approx(2.0, abs=0.02)
    assert estimate_freq(out, 0.1, 0.9) == pytest.approx(220, rel=0.03)
    assert estimate_freq(out, 1.1, 1.9) == pytest.approx(220, rel=0.03)   # 第二段没有变速


@requires_ffmpeg
def test_ensure_uniform_passes_through_clean_clips(settings, tmp_path):
    one = write_tone(tmp_path / "one.wav", seconds=0.2, rate=24000, freq=300)
    clips, target = ensure_uniform(settings, [clip_from_wav("a", one)], tmp_path / "work")
    assert target == 24000
    assert clips[0].path == one            # 已经统一 → 不重编码
    assert not (tmp_path / "work").exists()


@requires_ffmpeg
def test_ensure_uniform_converts_stereo_and_bit_depth(settings, tmp_path):
    from audiobook.render.ffmpeg import probe_wav, run_ffmpeg

    mono = write_tone(tmp_path / "mono.wav", seconds=0.3, rate=24000, freq=200)
    stereo = tmp_path / "stereo.wav"
    run_ffmpeg(settings, ["-i", str(mono), "-ac", "2", "-c:a", "pcm_s16le", str(stereo)])
    clips, target = ensure_uniform(settings, [clip_from_wav("a", stereo)], tmp_path / "work")
    assert target == 24000
    assert (clips[0].channels, clips[0].bits) == (1, 16)
    assert probe_wav(clips[0].path).duration == pytest.approx(0.3, abs=0.02)
