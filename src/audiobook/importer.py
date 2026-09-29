import time
import uuid
from pathlib import Path

from . import jobs, store
from .text.epub import epub_to_text


def read_source_text(source: Path) -> tuple[str, str]:
    """把书稿读成纯文本，返回 (文本, 格式)。

    txt 直读；epub 走 text/epub.py 抽正文。抽出来的文本会写进
    source/original.txt，下游（清洗/分章/分析）完全不用知道原文件是什么格式。
    """
    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return epub_to_text(path), "epub"
    if suffix in (".txt", ""):
        return path.read_text(encoding="utf-8"), "txt"
    raise ValueError(f"不支持的格式 {suffix or '（无扩展名）'}：只能导入 txt / epub")


def import_book(settings, conn, source: Path, title: str, book_id: str | None = None) -> str:
    book_id = book_id or uuid.uuid4().hex
    source = Path(source)
    raw, fmt = read_source_text(source)
    if not raw.strip():
        raise ValueError("书稿是空的：没读到任何正文")
    store.atomic_write_text(store.source_path(settings, book_id), raw)
    store.atomic_replace_json(
        store.book_dir(settings, book_id) / "book.json",
        {
            "id": book_id,
            "title": title,
            "source": str(source),
            "format": fmt,
            "created_at": int(time.time() * 1000),
            "status": "imported",
        },
    )
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (book_id, title, str(source), 0, "imported", int(time.time() * 1000)),
    )
    jobs.enqueue(conn, "chapter_split", book_id)
    return book_id
