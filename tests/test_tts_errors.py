from audiobook import store
from audiobook.analysis.issues import ISSUE_KINDS
from audiobook.config import get_settings
from audiobook.engines.errors import (
    TtsBadRef,
    TtsBadRequest,
    TtsBusy,
    TtsError,
    TtsOom,
    TtsUnavailable,
    TtsVoiceMissing,
    classify_status,
)


def test_classify_status_maps_service_codes_to_typed_errors():
    assert classify_status(429, "busy") is TtsBusy
    assert classify_status(503, "busy") is TtsBusy
    assert classify_status(503, "oom") is TtsOom
    assert classify_status(503, "not_loaded") is TtsUnavailable
    assert classify_status(404, "bad_ref") is TtsBadRef
    assert classify_status(400, "bad_request") is TtsBadRequest
    assert classify_status(500, "engine_error") is TtsError
    assert classify_status(418, None) is TtsError


def test_typed_errors_are_tts_errors():
    for cls in (TtsBusy, TtsOom, TtsUnavailable, TtsBadRef, TtsBadRequest, TtsVoiceMissing):
        assert issubclass(cls, TtsError)
    assert issubclass(TtsVoiceMissing, TtsBadRef)


def test_tts_settings_defaults(tmp_path):
    settings = get_settings(data_dir=tmp_path / "data")      # 不读开发机上的 data/settings.json
    assert settings.engine == "http"
    assert settings.tts_backend == "indextts"
    assert settings.synth_concurrency_max == 16
    assert settings.tts_breaker_seconds == 60.0
    assert settings.tts_max_line_chunk_chars == 0


def test_voice_ref_path_layout(settings):
    assert store.voice_ref_path(settings, "v_x").as_posix().endswith("voices/v_x/ref.wav")


def test_tts_issue_kinds_are_registered():
    for kind in ("tts_line_failed", "tts_ref_missing", "tts_endpoint_down", "audio_missing"):
        assert kind in ISSUE_KINDS
