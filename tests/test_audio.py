import wave

import pytest

from audiobook import audio
from fake_engine import FakeEngine


def _make(tmp_path, name, text, sample_rate=22050):
    engine = FakeEngine(sample_rate=sample_rate, ms_per_char=10.0)
    return engine.synthesize(text, "v1", None, tmp_path / name)


def test_wav_duration_matches_generated(tmp_path):
    result = _make(tmp_path, "a.wav", "一二三四五")
    assert audio.wav_duration(result.path) == pytest.approx(0.05, abs=1e-3)


def test_concat_with_pauses_inserts_silence_and_returns_total(tmp_path):
    a = _make(tmp_path, "a.wav", "一二三四五")
    b = _make(tmp_path, "b.wav", "六七八九十")
    out = tmp_path / "chapter.wav"
    total = audio.concat_with_pauses([(a.path, 300), (b.path, 0)], out)
    assert total == pytest.approx(0.4, abs=1e-3)
    assert audio.wav_duration(out) == pytest.approx(0.4, abs=1e-3)
    with wave.open(str(out)) as fh:
        assert fh.getframerate() == 22050


def test_concat_rejects_mixed_formats(tmp_path):
    a = _make(tmp_path, "a.wav", "一二三", sample_rate=22050)
    b = _make(tmp_path, "b.wav", "四五", sample_rate=24000)
    with pytest.raises(audio.FormatMismatch):
        audio.concat_with_pauses([(a.path, 0), (b.path, 0)], tmp_path / "x.wav")


def test_format_srt_time_and_write(tmp_path):
    assert audio.format_srt_time(0) == "00:00:00,000"
    assert audio.format_srt_time(3661.5) == "01:01:01,500"
    path = tmp_path / "c.srt"
    audio.write_srt([(0.0, 1.0, "第一句"), (1.3, 2.0, "第二句")], path)
    text = path.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:01,000\n第一句" in text
    assert text.strip().endswith("第二句")
