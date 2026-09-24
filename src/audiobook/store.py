import json
import os
from pathlib import Path


def book_dir(settings, book_id: str) -> Path:
    return settings.books_dir / book_id


def chapter_tag(index: int) -> str:
    return f"{index:04d}"


def source_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "source" / "original.txt"


def chapters_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "chapters.json"


def lines_path(settings, book_id: str, index: int) -> Path:
    return book_dir(settings, book_id) / "analysis" / "lines" / f"chapter_{chapter_tag(index)}.jsonl"


def audio_dir(settings, book_id: str, index: int) -> Path:
    return book_dir(settings, book_id) / "audio" / f"chapter_{chapter_tag(index)}"


def output_dir(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "output"


def issues_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "issues.jsonl"


def logs_dir(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "logs"


def llm_log_path(settings, book_id: str) -> Path:
    return logs_dir(settings, book_id) / "llm.jsonl"


def characters_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "analysis" / "characters.json"


def scenes_dir(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "analysis" / "scenes"


def scenes_path(settings, book_id: str, index: int) -> Path:
    return scenes_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.json"


def casting_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "voices" / "casting.json"


def voice_library_dir(settings) -> Path:
    return settings.voices_dir


def voice_path(settings, voice_id: str) -> Path:
    return settings.voices_dir / voice_id / "voice.json"


def voice_ref_path(settings, voice_id: str) -> Path:
    return settings.voices_dir / voice_id / "ref.wav"


def pronounce_path(settings) -> Path:
    return settings.data_dir / "pronounce.json"


def chapter_wav_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.wav"


def chapter_srt_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.srt"


def chapter_media_path(settings, book_id: str, index: int, ext: str) -> Path:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}{suffix}"


def chapter_render_meta_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.render.json"


def render_work_dir(settings, book_id: str, index: int) -> Path:
    return audio_dir(settings, book_id, index) / "_render"


def book_wav_path(settings, book_id: str) -> Path:
    return output_dir(settings, book_id) / "book.wav"


def book_srt_path(settings, book_id: str) -> Path:
    return output_dir(settings, book_id) / "book.srt"


def book_media_path(settings, book_id: str, ext: str) -> Path:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return output_dir(settings, book_id) / f"book{suffix}"


def export_target_dir(settings, book_id: str, out_dir=None) -> Path:
    return Path(out_dir) if out_dir else output_dir(settings, book_id)


def settings_overlay_path(settings) -> Path:
    return settings.data_dir / "settings.json"


def count_issues(settings, book_id: str) -> int:
    return len(read_jsonl(issues_path(settings, book_id)))


def chapter_state(settings, book_id: str, index: int) -> dict:
    """章节在流水线上的位置：empty → analyzed → synthesized → rendered。"""
    rows = read_jsonl(lines_path(settings, book_id, index))
    scenes = (read_json(scenes_path(settings, book_id, index), default={}) or {}).get("scenes") or []
    meta = read_json(chapter_render_meta_path(settings, book_id, index), default={}) or {}
    clips_dir = audio_dir(settings, book_id, index)
    segments = sum(1 for row in rows if (clips_dir / f"{row['id']}.wav").exists())
    if meta and chapter_wav_path(settings, book_id, index).exists():
        state = "rendered"
    elif segments:
        state = "synthesized"
    elif rows:
        state = "analyzed"
    else:
        state = "empty"
    return {
        "index": index,
        "scenes": len(scenes),
        "lines": len(rows),
        "segments": segments,
        "duration_sec": float(meta.get("duration") or 0.0),
        "rendered_at": meta.get("generated_at"),
        "state": state,
    }


def book_stats(settings, book_id: str) -> dict:
    chapters = (read_json(chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    total = len(chapters)
    analyzed = generated = 0
    duration = 0.0
    for chapter in chapters:
        detail = chapter_state(settings, book_id, int(chapter["index"]))
        analyzed += 1 if detail["lines"] else 0
        generated += 1 if detail["state"] == "rendered" else 0
        duration += detail["duration_sec"]
    if total == 0:
        state = "empty"
    elif generated == total:
        state = "ready"
    elif generated:
        state = "synthesizing"
    elif analyzed:
        state = "analyzed"
    else:
        state = "analyzing"
    return {
        "chapters": total,
        "analyzed": analyzed,
        "generated": generated,
        "duration_sec": round(duration, 2),
        "issues": count_issues(settings, book_id),
        "state": state,
    }


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_replace_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl_atomic(path: Path, rows) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    atomic_write_text(path, text)


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
