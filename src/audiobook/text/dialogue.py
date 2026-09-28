"""把正文切成「旁白 / 引语」交替的句子单元。

小说里的直接引语（引号内）和它前后的叙述是两种东西：前者要按角色配音，后者是旁白。
旧系统就是这么切的——引号内的内容单独成行，引号本身去掉，"X 说道"这类归属句留在旁白里。
不这么切，模型看到的是「“台词”王胖子一听，吃惊道。」这种混在一起的句子，
只能靠猜，于是大部分台词被标成旁白。

顺带挡住引号的非对话用法（`所谓的“天才”`）：不像人话的引语并回旁白，不单独成行。
"""

import re
from dataclasses import dataclass

from .split_chapters import split_span

OPENERS = {"“": "”", "「": "」", "『": "』", '"': '"'}

SPEECH_VERB = (
    r"(?:说道|问道|答道|喊道|叫道|笑道|怒道|喝道|叹道|低声道|沉声道|冷冷道|冷笑道|开口道|"
    r"说|道|问|答|喊|叫|骂|念|唱|吼|哼|叹|嘟囔|嘀咕)"
)
# 引语前面出现这些，说明是在"说话"
SPEECH_CUE = re.compile(rf"{SPEECH_VERB}[：:]?\s*$")
# 引语后面跟着的这些，说明刚才是"谁在说话"
ATTRIBUTION_AFTER = re.compile(rf"^[^。！？；：]{{0,14}}?{SPEECH_VERB}(?=[，。！？；：、\s”\"」』]|$)")
SENTENCE_END = re.compile(r"[。！？!?…]")
PAUSE_ONLY = re.compile(r"^[\s…\.\-—~～·]+$")


@dataclass(frozen=True)
class Unit:
    """一句话单元：kind 为 narration（旁白）或 dialogue（引语）。"""

    kind: str
    text: str
    paragraph: int = 0


def _looks_like_speech(prefix: str, inner: str, suffix: str) -> bool:
    """引号里的东西到底是不是"人说的话"。"""
    head = prefix.rstrip()
    if head.endswith(("：", ":")):
        return True
    if head and SPEECH_CUE.search(head[-8:]):
        return True
    if ATTRIBUTION_AFTER.match(suffix.lstrip()[:14]):
        return True
    if inner and PAUSE_ONLY.match(inner):
        return True  # “……”这种
    if SENTENCE_END.search(inner):
        return True  # “嗯。”“走！”这类短台词靠句末标点认出来
    # 没有句末标点的长引语（网文常把句号省在引号外）：够长且带逗号才算台词，
    # 免得把“绰号”“强调”这种短引语当成对白
    return len(inner) >= 10 and "，" in inner


def _paragraph_spans(line: str, carry: str | None) -> tuple[list[tuple[str, str]], str | None]:
    """切一段（一个自然段）；carry 是上一段没闭合的引号，返回 (片段, 新 carry)。"""
    spans: list[tuple[str, str]] = []
    buffer: list[str] = []
    index = 0
    length = len(line)

    if carry:  # 上一段话没说完，这一段开头仍然是引语
        end = line.find(carry)
        inner = (line if end == -1 else line[:end]).strip()
        if inner:
            spans.append(("dialogue", inner))
        if end == -1:
            return spans, carry
        index = end + 1

    while index < length:
        char = line[index]
        closer = OPENERS.get(char)
        if closer is None:
            buffer.append(char)
            index += 1
            continue
        prefix = "".join(buffer).strip()
        end = line.find(closer, index + 1)
        inner = line[index + 1 : end if end != -1 else length].strip()
        suffix = "" if end == -1 else line[end + 1 :]
        if _looks_like_speech(prefix, inner, suffix):
            if prefix:
                spans.append(("narration", prefix))
            if inner:
                spans.append(("dialogue", inner))
            buffer = []
            carry = None if end != -1 else closer
        else:  # 不是人话：连引号一起并回旁白
            buffer = [prefix, f"{char}{inner}{closer if end != -1 else ''}"]
            carry = None
        index = length if end == -1 else end + 1
    tail = "".join(buffer).strip()
    if tail:
        spans.append(("narration", tail))
    return spans, carry


def split_units(content: str) -> list[Unit]:
    """整章正文 → 句子单元列表（旁白 / 引语交替，保持段落顺序）。"""
    units: list[Unit] = []
    carry: str | None = None
    for paragraph, raw in enumerate(content.split("\n")):
        line = raw.strip()
        if not line:
            continue
        spans, carry = _paragraph_spans(line, carry)
        for kind, text in spans:
            for piece in split_span(text):
                if piece:
                    units.append(Unit(kind=kind, text=piece, paragraph=paragraph))
    return units
