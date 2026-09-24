from audiobook.text.prosody import derive_pause_ms, derive_rate


def test_derive_pause_by_punctuation():
    assert derive_pause_ms("他说完了。") == 300
    assert derive_pause_ms("他停顿了一下，") == 120
    assert derive_pause_ms("什么……") == 800
    assert derive_pause_ms("走了。", scene_switch=True) == 800
    assert derive_pause_ms("滚！", intensity=0.9) == 500


def test_derive_rate_by_delivery():
    assert derive_rate("shout") == 1.05
    assert derive_rate("whisper") == 0.92
    assert derive_rate("normal") == 1.0
