"""尾部静音裁剪：批量解码时短句会陪跑到最长那条结束，尾巴是一长串数字静音。"""

import pytest

np = pytest.importorskip("numpy")

from aiab_tts.backends.qwen3 import _trim_tail  # noqa: E402


def test_trim_tail_cuts_long_silence_and_keeps_a_short_fade():
    sr = 24000
    speech = (np.sin(np.linspace(0, 200, sr)) * 0.5).astype(np.float32)   # 1 秒有声
    tail = np.zeros(sr * 15, dtype=np.float32)                          # 15 秒静音
    out = _trim_tail(np.concatenate([speech, tail]), sr)

    seconds = len(out) / sr
    assert seconds == pytest.approx(1.35, abs=0.05)      # 1 秒语音 + 0.35 秒收束


def test_trim_tail_leaves_normal_clip_untouched():
    sr = 24000
    speech = (np.sin(np.linspace(0, 400, sr * 2)) * 0.5).astype(np.float32)
    speech = np.concatenate([speech, np.zeros(int(sr * 0.2), dtype=np.float32)])
    out = _trim_tail(speech, sr)
    assert len(out) == len(speech)      # 尾巴不到半秒：不动


def test_trim_tail_handles_all_silence():
    sr = 24000
    silent = np.zeros(sr * 3, dtype=np.float32)
    assert len(_trim_tail(silent, sr)) == len(silent)
