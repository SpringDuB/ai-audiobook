import json

import pytest

from audiobook import store
from audiobook.migrate.voices import build_voice_record, migrate_voices
from helpers import write_tone


def _full_voice(root, name="暗夜玫瑰"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "标签.json").write_text(
        json.dumps(
            {
                "name": name,
                "gender": "女",
                "ageGroup": "中年",
                "personality": ["魅惑/诱惑"],
                "genres": ["都市"],
                "mood": ["浪漫"],
                "speechRate": "慢",
                "voiceQuality": ["磁性"],
                "languageStyle": ["普通话"],
                "usageType": ["角色对话"],
                "description": "低沉磁性成熟女声",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / "试听文本.txt").write_text("弟弟，姐姐可不是什么好人。", encoding="utf-8")
    (directory / "情绪基调.txt").write_text("危险·诱惑", encoding="utf-8")
    (directory / "提示词.txt").write_text("低沉磁性的成熟女声", encoding="utf-8")
    (directory / "应用场景.txt").write_text("年下恋女主", encoding="utf-8")
    write_tone(directory / "参考音频.wav", seconds=0.3, rate=24000, freq=220)
    return directory


def _bare_voice(root, name="霸道总裁"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "提示词.txt").write_text("低沉磁性的成熟男声", encoding="utf-8")
    write_tone(directory / "参考音频.wav", seconds=0.2, rate=24000, freq=220)
    return directory


def _fake_library(tmp_path, extra=()):
    source = tmp_path / "library"
    source.mkdir()
    _full_voice(source, "暗夜玫瑰")
    _bare_voice(source, "霸道总裁")
    _full_voice(source, "王胖子")
    for name in extra:
        (source / name).mkdir()
    return source


def test_build_voice_record_maps_camel_case_tags(tmp_path):
    directory = _full_voice(tmp_path)
    record = build_voice_record(directory, "v001")
    assert (record["id"], record["name"], record["gender"], record["age_group"]) == ("v001", "暗夜玫瑰", "女", "中年")
    assert record["speech_rate"] == "慢" and record["voice_quality"] == ["磁性"]
    assert record["language_style"] == ["普通话"] and record["usage_type"] == ["角色对话"]
    assert record["ref_text"].startswith("弟弟") and record["mood_hint"] == "危险·诱惑"
    assert record["needs_review"] is False
    assert record["ref"]["sample_rate"] == 24000 and record["ref"]["file"] == "ref.wav"
    assert record["source"] == {"dir": "暗夜玫瑰", "tags_file": "标签.json", "wav": "参考音频.wav"}


def test_build_voice_record_flags_missing_tags(tmp_path):
    record = build_voice_record(_bare_voice(tmp_path), "v002")
    assert record["needs_review"] is True
    assert record["gender"] == "男"          # 从提示词里的"男声"推断
    assert record["age_group"] == "未知"
    assert record["source"]["tags_file"] is None


def test_migrate_voices_is_deterministic_and_idempotent(settings, tmp_path):
    source = _fake_library(tmp_path)
    first = migrate_voices(settings, source)
    assert first.migrated == ("v001", "v002", "v003")
    assert [p.parent.name for p in sorted(settings.voices_dir.glob("*/voice.json"))] == ["v001", "v002", "v003"]
    assert store.voice_ref_path(settings, "v001").exists()
    # 排序按目录名的码点：暗夜玫瑰 < 王胖子 < 霸道总裁
    records = {path.parent.name: store.read_json(path) for path in settings.voices_dir.glob("*/voice.json")}
    review = [voice_id for voice_id, record in records.items() if record["needs_review"]]
    assert first.needs_review == tuple(review) and len(review) == 1
    assert records[review[0]]["name"] == "霸道总裁"
    assert first.total_bytes > 0

    again = migrate_voices(settings, source)
    assert again.migrated == first.migrated
    assert (settings.voices_dir / "v001" / "ref.wav").read_bytes() == (
        source / "暗夜玫瑰" / "参考音频.wav"
    ).read_bytes()


def test_migrate_voices_skips_directories_without_wav(settings, tmp_path):
    source = _fake_library(tmp_path, extra=("空目录",))
    report = migrate_voices(settings, source)
    assert len(report.migrated) == 3 and report.skipped == ("空目录",)


def test_migrate_voices_dry_run_writes_nothing(settings, tmp_path):
    report = migrate_voices(settings, _fake_library(tmp_path), dry_run=True)
    assert report.dry_run is True and len(report.migrated) == 3
    assert not settings.voices_dir.exists()


def test_migrate_voices_missing_source_raises(settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate_voices(settings, tmp_path / "nope")
