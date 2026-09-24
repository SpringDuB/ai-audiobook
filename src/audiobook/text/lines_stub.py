from .prosody import derive_pause_ms, derive_rate
from .split_chapters import split_sentences


def line_id(chapter_index: int, scene_index: int, seq: int) -> str:
    return f"c{chapter_index:04d}-s{scene_index:02d}-l{seq:03d}"


def stub_lines(chapter_index: int, content: str, scene_index: int = 1, speaker: str = "旁白") -> list[dict]:
    """M0 的降级实现：整章一个场景、全部旁白。M1 由 Pass A/B/C 的结果替换，文件格式不变。"""
    rows: list[dict] = []
    for seq, sentence in enumerate(split_sentences(content), start=1):
        rows.append(
            {
                "id": line_id(chapter_index, scene_index, seq),
                "scene": f"c{chapter_index:04d}-s{scene_index:02d}",
                "speaker": speaker,
                "addressee": None,
                "text": sentence,
                "emotion": {"dominant": None, "intensity": None},
                "delivery": "normal",
                "pause_after_ms": derive_pause_ms(sentence),
                "rate": derive_rate("normal"),
                "pronounce": {},
            }
        )
    return rows
