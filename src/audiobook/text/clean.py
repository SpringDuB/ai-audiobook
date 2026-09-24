import re
from dataclasses import dataclass, field

from .split_chapters import match_chapter_title

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060\u00ad"), None)

NOISE_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"panlay", re.I),
    re.compile(r"浏览器访问"),
    re.compile(r"(更多|免费).{0,8}(网盘|资源|小说|电子书)"),
    re.compile(r"以下.{0,6}(小说网|读书网|看书)"),
    re.compile(r"^(笔趣阁|啃书小说网|顶点小说|无弹窗).*$"),
    re.compile(r"^[\s\-—_=*·.]{4,}$"),
    re.compile(r"^[\W_]{1,4}$"),
]

TITLE_SUFFIX = re.compile(
    r"[（(](求收藏|求推荐|求订阅|新书|加更|第[一二三四五六七八九十\d]+更|月票)[^）)]*[）)]\s*$"
)


@dataclass
class CleanResult:
    text: str
    dropped: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"dropped": 0, "kept": 0})


def clean_title(title: str) -> str:
    return TITLE_SUFFIX.sub("", title).strip()


def _is_noise(line: str) -> bool:
    return any(p.search(line) for p in NOISE_PATTERNS)


def _normalize(line: str) -> str:
    if match_chapter_title(line):
        return clean_title(line)
    return line


def clean_text(text: str) -> CleanResult:
    text = text.translate(ZERO_WIDTH)
    kept: list[str] = []
    dropped: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _is_noise(line):
            dropped.append(line)
            continue
        kept.append(_normalize(line))
    return CleanResult(
        text="\n\n".join(kept),
        dropped=dropped,
        stats={"dropped": len(dropped), "kept": len(kept)},
    )
