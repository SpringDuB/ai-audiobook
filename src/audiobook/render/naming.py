import re

from .. import store

_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')
_CHAPTER_PREFIX = re.compile(r"^第\s*\d+\s*章")


def safe_filename(name: str, *, limit: int = 60) -> str:
    cleaned = _UNSAFE.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit].strip()


def chapter_title(settings, book_id: str, index: int) -> str:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    for chapter in chapters:
        if int(chapter.get("index", -1)) == index:
            return str(chapter.get("title") or "")
    return ""


def chapter_label(settings, book_id: str, index: int) -> str:
    title = re.sub(r"\s+", " ", chapter_title(settings, book_id, index)).strip()
    if not title or title == f"chapter_{index}":
        return f"第{index}章"
    if not _CHAPTER_PREFIX.match(title):
        title = f"第{index}章 {title}"
    return title[:60].strip()


def book_title(settings, book_id: str) -> str:
    meta = store.read_json(store.book_dir(settings, book_id) / "book.json", default={}) or {}
    return str(meta.get("title") or "book")
