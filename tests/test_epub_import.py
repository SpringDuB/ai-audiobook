"""EPUB 导入：按 spine 顺序抽正文，落成 original.txt，下游不用改。"""

import zipfile
from pathlib import Path

import pytest

from audiobook import store
from audiobook.importer import import_book, read_source_text
from audiobook.text.epub import epub_to_text

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>占位标题</title>
<style>p { color: red; }</style></head>
<body><h1>{title}</h1><p>{body}</p></body></html>
"""


def make_epub(path: Path, chapters: list[tuple[str, str]], *, spine: list[str] | None = None) -> Path:
    """手搓一个最小 epub：container.xml → content.opf → 每章一个 xhtml。"""
    items = []
    for index, _ in enumerate(chapters):
        items.append(f'<item id="c{index}" href="c{index}.xhtml" media-type="application/xhtml+xml"/>')
    order = spine or [f"c{index}" for index in range(len(chapters))]
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试书</dc:title></metadata>'
        f"<manifest>{''.join(items)}</manifest>"
        f"<spine>{''.join(f'<itemref idref=&quot;{ref}&quot;/>' for ref in order)}</spine>"
        "</package>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        for index, (title, body) in enumerate(chapters):
            # 别用 str.format：模板里的 CSS 花括号会被当成占位符
            archive.writestr(f"OEBPS/c{index}.xhtml", CHAPTER.replace("{title}", title).replace("{body}", body))
    return path


def test_epub_to_text_keeps_spine_order_and_drops_markup(tmp_path):
    path = make_epub(
        tmp_path / "book.epub",
        [("第一章 起点", "他说：“走。”</p><p>她没有回头。"), ("第二章 归途", "风停了。")],
    )
    text = epub_to_text(path)
    assert [line for line in text.splitlines() if line.strip()] == [
        "第一章 起点",
        "他说：“走。”",
        "她没有回头。",
        "第二章 归途",
        "风停了。",
    ]
    assert "color: red" not in text and "占位标题" not in text  # head/style 不落进正文


def test_epub_to_text_rejects_broken_zip(tmp_path):
    broken = tmp_path / "bad.epub"
    broken.write_bytes(b"definitely not a zip")
    with pytest.raises(ValueError, match="EPUB"):
        epub_to_text(broken)


def test_read_source_text_supports_txt_and_epub(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("第一句。", encoding="utf-8")
    assert read_source_text(txt) == ("第一句。", "txt")
    epub = make_epub(tmp_path / "b.epub", [("第一章", "正文。")])
    text, fmt = read_source_text(epub)
    assert fmt == "epub" and "正文。" in text
    with pytest.raises(ValueError, match="不支持的格式"):
        read_source_text(tmp_path / "c.pdf")


def test_import_epub_writes_plain_text_source(settings, conn, tmp_path):
    epub = make_epub(tmp_path / "书.epub", [("第一章 开始", "张卫东说：“来了。”")])
    book_id = import_book(settings, conn, epub, title="测试书", book_id="epub-book")
    stored = store.source_path(settings, book_id)
    assert stored.read_text(encoding="utf-8").splitlines() == ["第一章 开始", "张卫东说：“来了。”"]
    meta = store.read_json(store.book_dir(settings, book_id) / "book.json")
    assert meta["format"] == "epub" and meta["title"] == "测试书"
    queued = conn.execute("SELECT kind FROM jobs WHERE book_id=?", (book_id,)).fetchall()
    assert [row["kind"] for row in queued] == ["chapter_split"]


def test_import_rejects_unsupported_suffix(settings, conn, tmp_path):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="不支持的格式"):
        import_book(settings, conn, path, title="x")
