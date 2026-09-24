import pytest

from audiobook.audio import wav_duration
from fake_engine import FakeEngine


def test_capabilities_declare_indextts_like_features():
    caps = FakeEngine().capabilities()
    assert caps.name == "fake"
    assert caps.emotions is True
    assert len(caps.emotion_dims) == 8
    assert caps.sample_rate == 22050


def test_synthesize_writes_wav_with_expected_duration(tmp_path):
    engine = FakeEngine(ms_per_char=20.0)
    result = engine.synthesize("一二三四五", "v1", None, tmp_path / "out.wav")
    assert result.duration == pytest.approx(0.1, abs=1e-3)
    assert wav_duration(result.path) == pytest.approx(0.1, abs=1e-3)
    assert result.sample_rate == 22050


def test_fail_on_injects_error(tmp_path):
    engine = FakeEngine(fail_on={"要失败"})
    with pytest.raises(RuntimeError):
        engine.synthesize("这句话要失败", "v1", None, tmp_path / "bad.wav")
