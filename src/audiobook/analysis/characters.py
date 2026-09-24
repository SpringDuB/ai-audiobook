import re

from .models import PassAOutput
from .prompts import PASS_A_SYSTEM, pass_a_user

SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def chunk_text(text: str, max_chars: int) -> list[str]:
    """按句子边界把正文切成不超过 max_chars 的分块，拼接后等于原文。"""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return [text] if text else []
    pieces = [piece for piece in SENTENCE_SPLIT.split(text) if piece]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > limit:  # 单句超长时硬切，保证不丢字
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if len(current) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


def extract_chapter(runner, *, settings, book_id: str, chapter_index: int, title: str, content: str) -> PassAOutput:
    chunks = chunk_text(content, settings.llm_chunk_chars) or [content]
    outputs = [
        runner.run(
            system=PASS_A_SYSTEM,
            user=pass_a_user(chapter_index, title, chunk),
            model_cls=PassAOutput,
            pass_name="A",
            book_id=book_id,
            chapter_index=chapter_index,
        )
        for chunk in chunks
    ]
    return merge_pass_a(outputs)


def merge_pass_a(outputs: list[PassAOutput]) -> PassAOutput:
    characters = [card for out in outputs for card in out.characters]
    relationships = [rel for out in outputs for rel in out.relationships]
    return PassAOutput(characters=characters, relationships=relationships)
