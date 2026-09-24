import time
import uuid
from pathlib import Path

from . import jobs, store


def import_book(settings, conn, txt_path: Path, title: str, book_id: str | None = None) -> str:
    book_id = book_id or uuid.uuid4().hex
    raw = Path(txt_path).read_text(encoding="utf-8")
    store.atomic_write_text(store.source_path(settings, book_id), raw)
    store.atomic_replace_json(
        store.book_dir(settings, book_id) / "book.json",
        {
            "id": book_id,
            "title": title,
            "source": str(txt_path),
            "created_at": int(time.time() * 1000),
            "status": "imported",
        },
    )
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (book_id, title, str(txt_path), 0, "imported", int(time.time() * 1000)),
    )
    jobs.enqueue(conn, "chapter_split", book_id)
    return book_id
