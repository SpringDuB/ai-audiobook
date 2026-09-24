import pytest

from audiobook.config import OVERLAY_KEYS, get_settings, load_overlay, save_overlay


def test_overlay_persists_whitelisted_keys(tmp_path):
    s = get_settings(data_dir=tmp_path / "data")
    save_overlay(s, {"loudness_target_lufs": -19.5, "llm_base_url": "http://127.0.0.1:9000/v1"})
    assert load_overlay(s) == {"loudness_target_lufs": -19.5, "llm_base_url": "http://127.0.0.1:9000/v1"}
    merged = get_settings(data_dir=tmp_path / "data")
    assert (merged.loudness_target_lufs, merged.llm_base_url) == (-19.5, "http://127.0.0.1:9000/v1")
    assert merged.llm_model == "deepseek-flash"


def test_overlay_rejects_unknown_and_secret_keys(tmp_path):
    s = get_settings(data_dir=tmp_path / "data")
    with pytest.raises(ValueError):
        save_overlay(s, {"not_a_setting": 1})
    with pytest.raises(ValueError):
        save_overlay(s, {"llm_api_key": "sk-x"})
    with pytest.raises(ValueError):
        save_overlay(s, {"data_dir": "D:/elsewhere"})
    assert "llm_api_key" not in OVERLAY_KEYS
    assert "data_dir" not in OVERLAY_KEYS


def test_overlay_wins_over_env_but_not_explicit_init(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_LOUDNESS_TARGET_LUFS", "-20")
    s = get_settings(data_dir=tmp_path / "data")
    save_overlay(s, {"loudness_target_lufs": -17.0})
    assert get_settings(data_dir=tmp_path / "data").loudness_target_lufs == -17.0
    assert get_settings(data_dir=tmp_path / "data", loudness_target_lufs=-21.0).loudness_target_lufs == -21.0


def test_overlay_partial_update_keeps_previous_keys(tmp_path):
    s = get_settings(data_dir=tmp_path / "data")
    save_overlay(s, {"pause_max_ms": 900, "pause_min_ms": 100})
    save_overlay(s, {"pause_max_ms": 1500})
    assert load_overlay(s) == {"pause_min_ms": 100, "pause_max_ms": 1500}
