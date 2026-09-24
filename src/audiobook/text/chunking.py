import re

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
