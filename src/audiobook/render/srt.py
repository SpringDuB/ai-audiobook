import re
from dataclasses import dataclass
from pathlib import Path

from ..store import atomic_write_text

TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000.0


def format_time(seconds: float) -> str:
    total = int(round(max(0.0, seconds) * 1000))
    hours, total = divmod(total, 3_600_000)
    minutes, total = divmod(total, 60_000)
    secs, millis = divmod(total, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(path: Path) -> list[Cue]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        match = TIME.search(lines[index])
        if not match:
            index += 1
            continue
        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        texts: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].strip():
            texts.append(lines[cursor].strip())
            cursor += 1
        if texts:
            cues.append(Cue(start=start, end=end, text="\n".join(texts)))
        index = cursor
    return cues


def render_srt(cues: list[Cue]) -> str:
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(f"{number}\n{format_time(cue.start)} --> {format_time(cue.end)}\n{cue.text}\n")
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: Path) -> None:
    atomic_write_text(Path(path), render_srt(cues))


def shift_cues(cues: list[Cue], offset: float, *, limit: float | None = None) -> list[Cue]:
    shifted: list[Cue] = []
    for cue in cues:
        start = cue.start + offset
        end = cue.end + offset
        if limit is not None:
            if start >= limit:
                continue
            end = min(end, limit)
        if end <= start:
            continue
        shifted.append(Cue(start=start, end=end, text=cue.text))
    return shifted
