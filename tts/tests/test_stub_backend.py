import io
import wave

from aiab_tts.backends.base import SynthesisRequest
from _stub_backend import StubBackend


def _request(text: str = "第一句。", rate: float = 1.0, seed: int | None = 7) -> SynthesisRequest:
    return SynthesisRequest(
        text=text, ref_path="ref.wav", ref_text="", lang="ZH",
        emo_vector=None, rate=rate, pronunciation={}, seed=seed,
    )


def _duration(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload)) as handle:
        return handle.getnframes() / handle.getframerate()


def test_duration_scales_with_text_and_rate():
    backend = StubBackend()
    base = backend.synthesize(_request("第一句。")).duration_sec
    faster = backend.synthesize(_request("第一句。", rate=2.0)).duration_sec
    longer = backend.synthesize(_request("第一句。第二句。")).duration_sec
    assert faster < base < longer
    assert _duration(backend.synthesize(_request()).audio) > 0
    assert backend.capabilities()["engine"] == "stub-tts"


def test_same_seed_is_reproducible():
    backend = StubBackend()
    assert backend.synthesize(_request(seed=3)).audio == backend.synthesize(_request(seed=3)).audio
    assert backend.synthesize(_request(seed=3)).audio != backend.synthesize(_request(seed=4)).audio


def test_oom_is_injectable():
    backend = StubBackend(oom_on={"爆炸"})
    try:
        backend.synthesize(_request("会爆炸的句子"))
    except RuntimeError as exc:
        assert "out of memory" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("应当抛出显存错误")
