import pytest

from audiobook import store
from audiobook.editing import apply_line_patch, invalidate_chapter


def _row(**extra):
    return {
        "id": "c0001-s01-l001",
        "text": "原句。",
        "speaker": "narrator",
        "speaker_name": "旁白",
        "emotion": {"dominant": "平静", "intensity": 0.3, "source": "scene"},
        **extra,
    }


def test_patch_text_marks_manual_source():
    patched = apply_line_patch(_row(), {"text": "改过的句子。"}, {"旁白": "narrator"})
    assert patched["text"] == "改过的句子。"
    assert patched["edited_at"] > 0


def test_patch_speaker_accepts_name_or_id():
    # names 的约定与 characters.json / API 一致：{role_id: 角色名}
    names = {"narrator": "旁白", "role_0001": "张卫东"}
    by_name = apply_line_patch(_row(), {"speaker": "张卫东"}, names)
    assert (by_name["speaker"], by_name["speaker_name"]) == ("role_0001", "张卫东")
    assert apply_line_patch(_row(), {"speaker": "role_0001"}, names)["speaker"] == "role_0001"
    # 保留原说话人（把现有名字传回来）不应改坏
    same = apply_line_patch(_row(), {"speaker": "旁白"}, names)
    assert (same["speaker"], same["speaker_name"]) == ("narrator", "旁白")


def test_patch_addressee_can_be_cleared():
    names = {"narrator": "旁白", "role_0001": "张卫东"}
    row = _row(addressee="role_0001", addressee_name="张卫东")
    cleared = apply_line_patch(row, {"addressee": ""}, names)
    assert (cleared["addressee"], cleared["addressee_name"]) == (None, None)
    assert apply_line_patch(row, {"addressee": "张卫东"}, names)["addressee"] == "role_0001"


def test_patch_emotion_and_pause_are_clamped():
    patched = apply_line_patch(_row(), {"emotion": "愤怒", "intensity": 1.6, "pause_after_ms": 99999}, {})
    assert patched["emotion"] == {"dominant": "愤怒", "intensity": 1.0, "source": "manual"}
    assert patched["pause_after_ms"] == 99999
    assert patched["pause_override_ms"] == 5000      # 导出侧上限，防止手滑写 10 分钟


def test_patch_delivery_is_validated():
    assert apply_line_patch(_row(), {"delivery": "shout"}, {})["delivery"] == "shout"
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"delivery": "咆哮"}, {})


def test_patch_rejects_unknown_fields_and_values():
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"not_a_field": 1}, {})
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"emotion": "不存在的情绪"}, {})
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"text": "   "}, {})
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"pause_after_ms": -5}, {})
    with pytest.raises(ValueError):
        apply_line_patch(_row(), {"speaker": "查无此人"}, {"narrator": "旁白"})


def test_invalidate_chapter_removes_render_meta_and_container(settings):
    store.atomic_write_bytes(store.chapter_render_meta_path(settings, "b1", 1), b"{}")
    store.atomic_write_bytes(store.chapter_media_path(settings, "b1", 1, ".mkv"), b"x")
    assert invalidate_chapter(settings, "b1", 1) is True
    assert not store.chapter_render_meta_path(settings, "b1", 1).exists()
    assert not store.chapter_media_path(settings, "b1", 1, ".mkv").exists()
    assert invalidate_chapter(settings, "b1", 1) is False
