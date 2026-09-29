"""EPUB → 纯文本（只用标准库，不引第三方依赖）。

EPUB 就是一个 zip：``META-INF/container.xml`` 指向 .opf，opf 里的
manifest/spine 给出阅读顺序，正文是 XHTML。这里按 spine 顺序抽正文，
每个块级元素（p / div / h1 / li …）落成一行 —— 分章器
（text/split_chapters.py）正是按"第X章"这类标题行切章的。

抽出来的是"干净文本"，导入后写进 source/original.txt，
后续清洗、分章、分析全部沿用原路径，不需要为 docx/epub 再分叉。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

# 这些标签里的内容不是正文，整段丢掉
SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}
# 这些标签代表"换行/分段"
BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "figure", "figcaption", "header", "footer",
}
BODY_SUFFIXES = (".xhtml", ".html", ".htm")
BLOCK_MARK = "\x00"


class _Extractor(HTMLParser):
    """把一个 XHTML 文档压成"一行一段"的纯文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002 - HTMLParser 的签名
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self._parts.append(BLOCK_MARK)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self._parts.append(BLOCK_MARK)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag in BLOCK_TAGS:
            self._parts.append(BLOCK_MARK)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        chunks = "".join(self._parts).split(BLOCK_MARK)
        lines = []
        for chunk in chunks:
            cleaned = re.sub(r"\s+", " ", chunk).strip()
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)


def html_to_text(markup: str) -> str:
    parser = _Extractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_member(archive: zipfile.ZipFile, name: str) -> str:
    """zip 里的成员名大小写/前导斜杠可能不一致，找不到时按 basename 兜底。"""
    try:
        data = archive.read(name)
    except KeyError:
        wanted = name.lstrip("/").lower()
        for candidate in archive.namelist():
            if candidate.lstrip("/").lower() == wanted:
                data = archive.read(candidate)
                break
        else:
            raise
    return data.decode("utf-8", errors="replace")


def _opf_path(archive: zipfile.ZipFile) -> str | None:
    try:
        container = _read_member(archive, "META-INF/container.xml")
    except KeyError:
        return None
    try:
        root = ElementTree.fromstring(container)
    except ElementTree.ParseError:
        return None
    for node in root.iter():
        if _local_name(node.tag) == "rootfile":
            path = (node.attrib.get("full-path") or "").strip()
            if path:
                return path
    return None


def _spine_hrefs(archive: zipfile.ZipFile, opf_path: str) -> list[str]:
    try:
        root = ElementTree.fromstring(_read_member(archive, opf_path))
    except (KeyError, ElementTree.ParseError):
        return []
    base = posixpath.dirname(opf_path)
    manifest: dict[str, str] = {}
    for node in root.iter():
        if _local_name(node.tag) != "item":
            continue
        item_id = (node.attrib.get("id") or "").strip()
        href = (node.attrib.get("href") or "").strip()
        if item_id and href:
            manifest[item_id] = href
    hrefs: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) != "itemref":
            continue
        href = manifest.get((node.attrib.get("idref") or "").strip())
        if not href:
            continue
        full = posixpath.join(base, href) if base else href
        hrefs.append(posixpath.normpath(full).lstrip("/"))
    return hrefs


def _fallback_hrefs(archive: zipfile.ZipFile) -> list[str]:
    names = [
        name for name in sorted(archive.namelist()) if name.lower().endswith(BODY_SUFFIXES)
    ]
    return [name for name in names if not name.startswith("__MACOSX/")]


def epub_to_text(path: Path) -> str:
    """把 epub 抽成纯文本；解析不出正文就抛 ValueError（上层转成 400）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            opf = _opf_path(archive)
            hrefs = _spine_hrefs(archive, opf) if opf else []
            if not hrefs:
                hrefs = _fallback_hrefs(archive)
            sections: list[str] = []
            for href in hrefs:
                if not href.lower().endswith(BODY_SUFFIXES):
                    continue
                try:
                    markup = _read_member(archive, href)
                except KeyError:
                    continue
                text = html_to_text(markup)
                if text.strip():
                    sections.append(text)
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 EPUB 文件（压缩包打不开）") from exc
    if not sections:
        raise ValueError("这个 EPUB 里没找到正文，换一个版本试试")
    return "\n\n".join(sections)
