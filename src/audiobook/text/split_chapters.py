import re
from dataclasses import dataclass

CHAPTER_PATTERNS = [
    re.compile(r"^第[零一二三四五六七八九十百千万〇0-9]+[章节回卷部篇]\s*\S*"),
    re.compile(r"^[Cc]hapter\s+\d+.*"),
    re.compile(r"^卷[零一二三四五六七八九十百千万〇0-9]+\s*\S*"),
]

SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")
WORD_CHARS = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
CLOSING_MARKS = "”\"’』」）)】]"


@dataclass
class Chapter:
    index: int
    title: str
    content: str


def match_chapter_title(line: str) -> bool:
    line = line.strip()
    return bool(line) and any(p.match(line) for p in CHAPTER_PATTERNS)


def split_span(text: str) -> list[str]:
    """把一段连续文本按中文标点断句；只由标点/引号组成的碎片并回上一句。

    引语分片（text/dialogue.py）也走这里，保证"引号里怎么断句"和正文完全一致。
    """
    merged: list[str] = []
    for piece in (item.strip() for item in SENTENCE_SPLIT.split(text)):
        if not piece:
            continue
        if merged:
            stripped = piece.lstrip(CLOSING_MARKS)
            moved = piece[: len(piece) - len(stripped)]
            if moved and stripped:
                merged[-1] += moved  # “好。”他说。 → 句尾引号归上一句
                piece = stripped
        if merged and not WORD_CHARS.search(piece):
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged


def split_sentences(content: str) -> list[str]:
    """按中文标点断句；只由标点/引号组成的小片段（如句尾的 `”`）并回上一句。"""
    parts: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts.extend(split_span(line))
    return parts


def split_chapters(text: str) -> list[Chapter]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    chapters: list[Chapter] = []
    preface: list[str] = []
    current_title: str | None = None
    current_body: list[str] = []

    for line in lines:
        if match_chapter_title(line):
            if current_title is not None:
                chapters.append(Chapter(index=0, title=current_title, content="\n".join(current_body).strip()))
            current_title = line
            current_body = []
        elif current_title is None:
            preface.append(line)
        else:
            current_body.append(line)
    if current_title is not None:
        chapters.append(Chapter(index=0, title=current_title, content="\n".join(current_body).strip()))
    if preface:
        chapters.insert(0, Chapter(index=0, title="前言", content="\n".join(preface).strip()))
    for offset, chapter in enumerate(chapters):
        chapter.index = offset
    return chapters
