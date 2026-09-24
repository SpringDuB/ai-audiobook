import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

from .. import store

MANIFEST = "snapshot.json"
KEEP_SUFFIXES = (".json", ".jsonl")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_snapshot(settings, book_id: str, out: Path) -> dict:
    """把一本书的 JSON/JSONL 状态打成一个 zip（不含音频）。"""
    book_dir = store.book_dir(settings, book_id)
    if not book_dir.exists():
        raise FileNotFoundError(f"书不存在：{book_id}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    names = []
    for path in sorted(book_dir.rglob("*")):
        if not path.is_file() or path.suffix not in KEEP_SUFFIXES:
            continue
        if path.name in {MANIFEST}:
            continue
        relative = path.relative_to(book_dir).as_posix()
        data = path.read_bytes()
        entries.append({"path": relative, "bytes": len(data), "sha256": _sha256_bytes(data)})
        names.append(relative)
    meta = store.read_json(book_dir / "book.json", default={}) or {}
    manifest = {
        "format": 1,
        "book_id": book_id,
        "title": meta.get("title"),
        "exported_at": int(time.time() * 1000),
        "files": len(entries),
        "names": names,
        "entries": entries,
    }
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            archive.write(book_dir / entry["path"], entry["path"])
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    tmp.replace(out)
    return manifest


def _replace_book_id(settings, book_id: str, target_id: str) -> None:
    book_dir = store.book_dir(settings, target_id)
    for path in sorted(book_dir.rglob("*")):
        if not path.is_file() or path.suffix not in KEEP_SUFFIXES:
            continue
        payload = store.read_json(path, default=None) if path.suffix == ".json" else None
        if isinstance(payload, dict) and payload.get("book_id") == book_id:
            payload["book_id"] = target_id
            store.atomic_replace_json(path, payload)
        elif isinstance(payload, dict) and payload.get("id") == book_id and path.name == "book.json":
            payload["id"] = target_id
            store.atomic_replace_json(path, payload)


def import_snapshot(settings, conn, path: Path, *, force: bool = False, book_id: str | None = None) -> dict:
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
        source_id = manifest["book_id"]
        target_id = book_id or source_id
        book_dir = store.book_dir(settings, target_id)
        if book_dir.exists() and not force:
            raise FileExistsError(f"目标书目录已存在：{target_id}（用 force 覆盖）")
        restored = 0
        for entry in manifest["entries"]:
            data = archive.read(entry["path"])
            if _sha256_bytes(data) != entry["sha256"]:
                raise ValueError(f"快照文件校验失败：{entry['path']}")
            store.atomic_write_bytes(book_dir / entry["path"], data)
            restored += 1
    if target_id != source_id:
        _replace_book_id(settings, source_id, target_id)
    meta = store.read_json(book_dir / "book.json", default={}) or {}
    title = meta.get("title") or manifest.get("title") or target_id
    if conn is not None:
        row = conn.execute("SELECT id FROM books WHERE id=?", (target_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO books(id, title, source_path, chapter_count, status, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    target_id,
                    title,
                    str(store.source_path(settings, target_id)),
                    len((store.read_json(store.chapters_path(settings, target_id), default={}) or {}).get("chapters") or []),
                    "imported",
                    int(time.time() * 1000),
                ),
            )
        else:
            conn.execute("UPDATE books SET title=? WHERE id=?", (title, target_id))
    return {"book_id": target_id, "source_book_id": source_id, "restored": restored, "title": title}


def default_snapshot_path(settings, book_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(settings.data_dir) / "snapshots" / f"{book_id}-{stamp}.zip"
