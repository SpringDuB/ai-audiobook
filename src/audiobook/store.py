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


def pronounce_path(settings) -> Path:
    return settings.data_dir / "pronounce.json"


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
