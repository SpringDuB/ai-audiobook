import re
from dataclasses import dataclass

CHAPTER_PATTERNS = [
    re.compile(r"^第[零一二三四五六七八九十百千万〇0-9]+[章节回卷部篇]\s*\S*"),
    re.compile(r"^[Cc]hapter\s+\d+.*"),
    re.compile(r"^卷[零一二三四五六七八九十百千万〇0-9]+\s*\S*"),
]

SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")


@dataclass
class Chapter:
    index: int
    title: str
    content: str


def match_chapter_title(line: str) -> bool:
    line = line.strip()
    return bool(line) and any(p.match(line) for p in CHAPTER_PATTERNS)


def split_sentences(content: str) -> list[str]:
    parts: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts.extend(piece.strip() for piece in SENTENCE_SPLIT.split(line) if piece.strip())
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
