import pytest

from audiobook.engines.errors import TtsUnavailable
from audiobook.engines.fake import FakeEngine
from audiobook.engines.factory import build_engine
from audiobook.engines.pool import TtsPool


def test_build_engine_defaults_to_fake(settings):
    assert isinstance(build_engine(settings), FakeEngine)


def test_build_engine_returns_pool_for_http(settings):
    settings = settings.model_copy(update={"engine": "http", "tts_endpoints": ["http://a.local", "http://b.local"]})
    engine = build_engine(settings)
    assert isinstance(engine, TtsPool)
    assert [state.base_url for state in engine.states] == ["http://a.local", "http://b.local"]
    engine.close()


def test_build_engine_requires_endpoints_for_http(settings):
    with pytest.raises(TtsUnavailable):
        build_engine(settings.model_copy(update={"engine": "http", "tts_endpoints": []}))


def test_build_engine_rejects_unknown_name(settings):
    with pytest.raises(ValueError):
        build_engine(settings.model_copy(update={"engine": "omnivoice"}))
