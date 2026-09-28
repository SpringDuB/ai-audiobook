from audiobook.text.prosody import derive_pause_ms, derive_rate


def test_derive_pause_by_punctuation():
    assert derive_pause_ms("他说完了。") == 300
    assert derive_pause_ms("他停顿了一下，") == 120
    assert derive_pause_ms("什么……") == 800
    assert derive_pause_ms("滚！", intensity=0.9) == 500


def test_derive_rate_by_delivery():
    assert derive_rate("shout") == 1.05
    assert derive_rate("whisper") == 0.92
    assert derive_rate("normal") == 1.0


def test_derive_rate_follows_emotion():
    """情绪参与语速：低落/忧郁要慢，激动/紧张要快；平静不改变语速。"""
    assert derive_rate("normal", {"dominant": "忧郁", "intensity": 0.6}) == 0.92
    assert derive_rate("normal", {"dominant": "喜悦", "intensity": 0.6}) == 1.04
    assert derive_rate("normal", {"dominant": "平静", "intensity": 0.3}) == 1.0
    assert derive_rate("shout", {"dominant": "愤怒", "intensity": 1.0}) <= 1.15
