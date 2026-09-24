import os
from dataclasses import dataclass
from pathlib import Path

from .. import store
from ..render.ffmpeg import probe_wav

TAG_KEYS = {
    "name",
    "gender",
    "ageGroup",
    "personality",
    "genres",
    "mood",
    "speechRate",
    "voiceQuality",
    "languageStyle",
    "usageType",
    "description",
}
SNAKE = {
    "ageGroup": "age_group",
    "speechRate": "speech_rate",
    "voiceQuality": "voice_quality",
    "languageStyle": "language_style",
    "usageType": "usage_type",
}
TEXT_FILES = {
    "ref_text": "试听文本.txt",
    "mood_hint": "情绪基调.txt",
    "prompt": "提示词.txt",
    "scenes": "应用场景.txt",
}


@dataclass(frozen=True)
class VoiceMigrationReport:
    source: Path
    target_root: Path
    migrated: tuple[str, ...]
    skipped: tuple[str, ...]
    needs_review: tuple[str, ...]
    total_bytes: int
    dry_run: bool = False


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _guess_gender(prompt: str, name: str) -> str:
    haystack = f"{prompt} {name}"
    for token in ("男声", "男生", "男性", "男"):
        if token in haystack:
            return "男"
    for token in ("女声", "女生", "女性", "女"):
        if token in haystack:
            return "女"
    return "未知"


def reference_wav(directory: Path) -> Path | None:
    preferred = directory / "参考音频.wav"
    if preferred.exists():
        return preferred
    fallback = directory / "ref.wav"
    if fallback.exists():
        return fallback
    candidates = sorted(path for path in directory.glob("*.wav") if path.is_file())
    return candidates[0] if candidates else None


def build_voice_record(directory: Path, voice_id: str) -> dict:
    tags_path = directory / "标签.json"
    tags = store.read_json(tags_path, default={}) or {}
    record: dict = {"id": voice_id, "name": str(tags.get("name") or directory.name)}
    for key, value in tags.items():
        if key == "name" or key not in TAG_KEYS:
            continue
        record[SNAKE.get(key, key)] = value
    prompt = _read_text(directory / "提示词.txt")
    record.setdefault("gender", _guess_gender(prompt, record["name"]))
    record.setdefault("age_group", "未知")
    for field, filename in TEXT_FILES.items():
        text = _read_text(directory / filename)
        if text:
            record[field] = text
    record["needs_review"] = not tags_path.exists()
    wav = reference_wav(directory)
    assert wav is not None  # 调用方已过滤没有 wav 的目录
    info = probe_wav(wav)
    record["source"] = {
        "dir": directory.name,
        "tags_file": tags_path.name if tags_path.exists() else None,
        "wav": wav.name,
    }
    record["ref"] = {
        "file": "ref.wav",
        "bytes": wav.stat().st_size,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "duration": round(info.duration, 3),
    }
    return record


def _copy_atomic(source: Path, target: Path) -> None:
    store.atomic_write_bytes(target, source.read_bytes())


def migrate_voices(settings, source, *, dry_run: bool = False, force: bool = False) -> VoiceMigrationReport:
    """把旧音色库搬成 data/voices/<voiceId>/（voice.json + ref.wav），幂等。"""
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(f"音色源目录不存在：{source}")
    directories = sorted((path for path in source.iterdir() if path.is_dir()), key=lambda path: path.name)
    migrated: list[str] = []
    skipped: list[str] = []
    needs_review: list[str] = []
    total_bytes = 0
    index = 0
    for directory in directories:
        wav = reference_wav(directory)
        if wav is None:
            skipped.append(directory.name)
            continue
        index += 1
        voice_id = f"v{index:03d}"
        record = build_voice_record(directory, voice_id)
        migrated.append(voice_id)
        total_bytes += int(record["ref"]["bytes"])
        if record["needs_review"]:
            needs_review.append(voice_id)
        if dry_run:
            continue
        target_dir = settings.voices_dir / voice_id
        target_dir.mkdir(parents=True, exist_ok=True)
        store.atomic_replace_json(target_dir / "voice.json", record)
        ref_path = target_dir / "ref.wav"
        if force or not ref_path.exists() or ref_path.stat().st_size != wav.stat().st_size:
            _copy_atomic(wav, ref_path)
    return VoiceMigrationReport(
        source=source,
        target_root=Path(settings.voices_dir),
        migrated=tuple(migrated),
        skipped=tuple(skipped),
        needs_review=tuple(needs_review),
        total_bytes=total_bytes,
        dry_run=dry_run,
    )
