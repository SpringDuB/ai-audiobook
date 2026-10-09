"""整章提取：把正文交给大模型，直出「每句话 + 说话人 + 对白情绪」。

分句、说话人归属、"X：" 前缀的剥离全部由模型完成，代码只负责：
长章节的窗口切分（按段落，不碰句内结构）、严格 JSON 校验、
失败兜底（整段按旁白保留原文）与原文完整性检查（改写/大段丢失必须可见）。
"""

import re
from dataclasses import dataclass, field

from ..llm.runner import LlmJsonError
from .models import ExtractionOutput, SpokenLine
from .prompts import EXTRACT_SYSTEM, extract_user

EXTRACT_PASS = "extract"

_WHITESPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[。！？!?…”」』]")


@dataclass
class ChapterExtraction:
    lines: list[SpokenLine]
    issues: list[dict] = field(default_factory=list)
    windows: int = 0


def window_text(content: str, max_chars: int) -> list[str]:
    """按段落（行）切窗口，保证不切开句子；单段超长时才硬切。"""
    limit = max(1, int(max_chars))
    if len(content) <= limit:
        return [content] if content else []
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for raw in content.split("\n"):
        line = raw
        while len(line) > limit:  # 单段超长：硬切，保证不丢字
            if current:
                windows.append("\n".join(current))
                current, size = [], 0
            windows.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            windows.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        windows.append("\n".join(current))
    return windows


def _normalized(text: str) -> str:
    return _WHITESPACE.sub("", text or "")


def _is_subsequence(needle: str, haystack: str) -> bool:
    cursor = 0
    for char in needle:
        cursor = haystack.find(char, cursor)
        if cursor < 0:
            return False
        cursor += 1
    return True


def text_integrity(source: str, lines: list[SpokenLine]) -> dict:
    """检查提取结果有没有改写原文（不是子序列）或大段丢失（句末标点骤减）。"""
    joined = "".join(line.text for line in lines)
    source_ends = len(_SENTENCE_END.findall(source))
    out_ends = len(_SENTENCE_END.findall(joined))
    ratio = round(out_ends / source_ends, 3) if source_ends else 1.0
    subsequence = _is_subsequence(_normalized(joined), _normalized(source))
    return {"ok": subsequence and ratio >= 0.6, "subsequence": subsequence, "ratio": ratio}


def extract_chapter(
    runner,
    *,
    book_id: str,
    chapter_index: int,
    title: str,
    content: str,
    window_chars: int,
    known_names: list[str] | None = None,
    on_window=None,
    cancel_check=None,
) -> ChapterExtraction:
    windows = window_text(content, window_chars)
    if not windows:
        raise RuntimeError(f"第 {chapter_index} 章没有可提取的正文")
    names = [name for name in (known_names or []) if name]
    lines: list[SpokenLine] = []
    issues: list[dict] = []
    for position, window in enumerate(windows, start=1):
        if cancel_check is not None:
            # 上一段跑完、下一段还没发：用户在这中间点了取消就直接停
            cancel_check()
        try:
            output = runner.run(
                system=EXTRACT_SYSTEM,
                user=extract_user(
                    chapter_index,
                    title,
                    window,
                    known_roles=names,
                    window=position,
                    windows=len(windows),
                ),
                model_cls=ExtractionOutput,
                pass_name=EXTRACT_PASS,
                book_id=book_id,
                chapter_index=chapter_index,
                cancel_check=cancel_check,
                # 顶层是数组：JSON mode（response_format=json_object）与它天然冲突，
                # 网关会逼模型把数组包成对象、甚至回显 {"type":"json_object"}。
                json_mode=False,
            )
        except LlmJsonError as exc:
            issues.append(
                {
                    "kind": "extract_window_failed",
                    "reason": f"第 {position}/{len(windows)} 段提取失败：{exc}",
                    "fallback": "这一段整段按旁白处理（原文保留，说话人与情绪丢失）",
                    "detail": {"window": position, "chars": len(window)},
                }
            )
            lines.append(SpokenLine(text=window, role="旁白"))
        else:
            for item in output.root:
                if not item.text:
                    continue
                lines.append(item)
                if item.role not in names:
                    names.append(item.role)
        if on_window is not None:
            on_window(position, len(windows), f"第 {position}/{len(windows)} 段")
    integrity = text_integrity(content, lines)
    if not integrity["ok"]:
        issues.append(
            {
                "kind": "extract_text_drift",
                "reason": "提取结果与原文对不上（被改写或成段丢失）",
                "fallback": "按模型输出落盘，请人工复核这一章",
                "detail": integrity,
            }
        )
    return ChapterExtraction(lines=lines, issues=issues, windows=len(windows))


def dump_extraction(result: ChapterExtraction) -> dict:
    return {
        "windows": result.windows,
        "lines": [line.model_dump() for line in result.lines],
    }


def load_extraction(payload: dict | None) -> list[SpokenLine]:
    return [SpokenLine.model_validate(item) for item in ((payload or {}).get("lines") or [])]


def is_legacy_extraction(payload: dict | None) -> bool:
    """是不是"没有逐句表演描述"的老格式（换 Qwen3-TTS 之前的结果）。

    老格式每条带 emotion、没有 voice。这种章节要重新提取一次，否则整章都只能拿
    角色基础描述去合成，逐句的语气就没了。
    """
    rows = (payload or {}).get("lines") or []
    if not rows:
        return False
    has_voice = any(str((row or {}).get("voice") or "").strip() for row in rows)
    has_emotion = any((row or {}).get("emotion") for row in rows)
    return has_emotion and not has_voice
