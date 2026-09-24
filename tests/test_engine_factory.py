import pytest

from audiobook.engines.errors import TtsUnavailable
from audiobook.engines.factory import build_engine
from audiobook.engines.pool import TtsPool


def test_build_engine_defaults_to_http_pool(settings):
    engine = build_engine(settings.model_copy(update={"tts_endpoints": ["http://127.0.0.1:8020"]}))
    assert isinstance(engine, TtsPool)


def test_build_engine_requires_endpoints(settings):
    with pytest.raises(TtsUnavailable, match="一键启动"):
        build_engine(settings.model_copy(update={"engine": "http", "tts_endpoints": []}))


def test_build_engine_rejects_removed_fake_engine(settings):
    """fake 引擎已从产品里删掉：老 .env 写 AB_ENGINE=fake 时要给出明确指引。"""
    with pytest.raises(ValueError, match="fake 引擎已移除"):
        build_engine(settings.model_copy(update={"engine": "fake"}))


def test_build_engine_rejects_unknown_name(settings):
    with pytest.raises(ValueError, match="未知引擎"):
        build_engine(settings.model_copy(update={"engine": "omnivoice"}))
