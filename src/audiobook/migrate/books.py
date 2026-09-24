import hashlib
import shutil
from pathlib import Path

from .. import store
from ..handlers.split import split_book
from ..importer import import_book
from .compare import compare_chapters, compare_roles


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _legacy_chapters(legacy_dir: Path | None) -> tuple[list[dict], list[str]]:
    if not legacy_dir:
        return [], []
    path = Path(legacy_dir) / "chapters.json"
    payload = store.read_json(path, default=[]) or []
    rows = payload if isinstance(payload, list) else payload.get("chapters") or []
    return rows, sorted(item.name for item in Path(legacy_dir).glob("roles_*.json"))


def migrate_book(
    settings,
    conn,
    txt: Path,
    *,
    title: str,
    legacy_dir: Path | None = None,
    cover: Path | None = None,
    book_id: str | None = None,
) -> dict:
    """把旧系统的一本书搬进来：清洗分章 → 封面 → 旧件留档 → 对照报告。"""
    txt = Path(txt)
    imported = import_book(settings, conn, txt, title=title, book_id=book_id)
    book_dir = store.book_dir(settings, imported)
    # 迁移要当场给出分章对照，所以同步分章并撤掉刚入队的 chapter_split
    chapters = split_book(settings, conn, imported)
    conn.execute(
        "DELETE FROM jobs WHERE kind='chapter_split' AND book_id=? AND status='queued'", (imported,)
    )
    old_chapters, roles_files = _legacy_chapters(legacy_dir)

    if cover:
        cover = Path(cover)
        if cover.exists():
            store.atomic_write_bytes(book_dir / "cover.png", cover.read_bytes())
            meta = store.read_json(book_dir / "book.json", default={}) or {}
            meta["cover"] = "cover.png"
            store.atomic_replace_json(book_dir / "book.json", meta)

    if legacy_dir:
        target = book_dir / "legacy"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("chapters.json", *roles_files):
            source = Path(legacy_dir) / name
            if source.exists():
                store.atomic_write_bytes(target / name, source.read_bytes())

    report = {
        "book_id": imported,
        "title": title,
        "source": {"path": str(txt), "bytes": txt.stat().st_size, "sha256": _sha256(txt)},
        "legacy": {
            "dir": str(legacy_dir) if legacy_dir else None,
            "roles_files": roles_files,
            "chapters_file": bool(legacy_dir and (Path(legacy_dir) / "chapters.json").exists()),
        },
        "chapters": compare_chapters(old_chapters, chapters),
        "roles_compare": None,
        "cover": bool(cover and (book_dir / "cover.png").exists()),
    }
    store.atomic_replace_json(book_dir / "migration.json", report)
    return report


def compare_book_roles(settings, book_id: str, legacy_dir: Path) -> dict:
    """分析跑完后，用旧 roles_*.json 复核新系统的说话人标注。"""
    legacy_dir = Path(legacy_dir)
    old_roles = []
    for path in sorted(legacy_dir.glob("roles_*.json")):
        payload = store.read_json(path, default=[]) or []
        old_roles.append(payload if isinstance(payload, list) else [])
    new_lines = {}
    for path in sorted(store.book_dir(settings, book_id).glob("analysis/lines/chapter_*.jsonl")):
        new_lines[int(path.stem.rsplit("_", 1)[-1])] = store.read_jsonl(path)
    result = compare_roles(old_roles, new_lines)
    report_path = store.book_dir(settings, book_id) / "migration.json"
    report = store.read_json(report_path, default={}) or {"book_id": book_id}
    report["roles_compare"] = result
    store.atomic_replace_json(report_path, report)
    return result
