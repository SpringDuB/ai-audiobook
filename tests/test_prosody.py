from audiobook.text import lines_stub
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


def test_stub_lines_ids_and_fields():
    rows = lines_stub.stub_lines(1, "第一句。第二句。")
    assert [r["id"] for r in rows] == ["c0001-s01-l001", "c0001-s01-l002"]
    assert rows[0]["scene"] == "c0001-s01"
    assert rows[0]["speaker"] == "旁白"
    assert rows[0]["pause_after_ms"] == 300
    assert rows[0]["rate"] == 1.0
    assert rows[0]["emotion"] == {"dominant": None, "intensity": None}
