"""整章分析：角色 + 逐句情感一趟 LLM 直出。

为什么合并：角色和情感本来就是一回事——"这句话是谁说的、他心里什么滋味"要靠整章上下文
（谁在场、刚才发生了什么、他和对方什么关系）才能判准。拆成"先抽角色、再单独给句子打情绪"
等于让模型蒙着眼睛标情绪，对白还容易被当成旁白。

超长章节按窗口切成多次调用（每行编号仍是"本章第几句"，跨窗口连续），
但每次都会把这章全文一起给模型当上下文；窗口之间还会带上已经出场的角色名，保证叫法一致。

LLM 只吐"角色名 + 标注"；把角色名映射成 role_id 是本地的事（materialize），
因为那时全书角色表已经聚合好了。
"""

from dataclasses import dataclass, field

from ..llm.runner import LlmJsonError
from ..text.dialogue import Unit, split_units
from .attribution import hints_for_names, names_from_payload
from .characters import NARRATOR_ID, characters_index, resolve_speaker, role_name
from .derive import derive_line, line_id
from .models import NARRATOR_NAMES, ChapterAnalysis, LineAnnotation
from .prompts import CHAPTER_ANALYSIS_SYSTEM, chapter_analysis_user

DEFAULT_SEGMENT = 1


@dataclass
class ChapterResult:
    analysis: ChapterAnalysis
    issues: list[dict] = field(default_factory=list)


def dump_chapter_analysis(analysis: ChapterAnalysis) -> dict:
    """落盘用的 JSON（关系用 from/to，和角色表里的写法一致）。"""
    return {
        "characters": [card.model_dump() for card in analysis.characters],
        "relationships": [rel.model_dump(by_alias=True) for rel in analysis.relationships],
        "lines": [line.model_dump() for line in analysis.lines],
    }


def load_chapter_analysis(payload: dict | None) -> ChapterAnalysis:
    return ChapterAnalysis.model_validate(payload or {})


def _text_of(item) -> str:
    return item.text if isinstance(item, Unit) else str(item)


def number_units(units: list, hints: list[str | None] | None = None, start: int = 1) -> str:
    """给单元编号（编号 = 本章第几句）并标出旁白/对白与说话人提示。"""
    rows: list[str] = []
    for offset, unit in enumerate(units):
        kind = getattr(unit, "kind", "narration")
        marker = "对白" if kind == "dialogue" else "旁白"
        hint = hints[offset] if hints and offset < len(hints) else None
        tag = f"[{marker}｜说话人提示：{hint}]" if hint else f"[{marker}]"
        rows.append(f"{start + offset}. {tag} {_text_of(unit)}")
    return "\n".join(rows)


def windowed(items: list, max_chars: int, max_sentences: int = 80) -> list[tuple[int, int]]:
    """把整章单元切成窗口（左闭右开、不重叠）。

    同时限字符数和句数：模型要逐句输出记录，窗口太大输出会被 max_output_tokens 截断。
    """
    limit = max(1, int(max_chars))
    cap = max(1, int(max_sentences))
    windows: list[tuple[int, int]] = []
    start, size = 0, 0
    for index, item in enumerate(items):
        length = len(_text_of(item))
        if size and (size + length > limit or index - start >= cap):
            windows.append((start, index))
            start, size = index, 0
        size += length
    if start < len(items):
        windows.append((start, len(items)))
    return windows


def analyze_chapter(
    runner,
    *,
    settings,
    book_id: str,
    chapter_index: int,
    title: str,
    content: str,
    units: list[Unit] | None = None,
    known_names: list[str] | None = None,
    on_window=None,
) -> ChapterResult:
    """整章跑一次（超长则分窗口）：产出角色 + 关系 + 每句的说话人/情绪标注。"""
    units = units if units is not None else split_units(content)
    if not units:
        raise RuntimeError(f"第 {chapter_index} 章没有可分析的句子")
    windows = windowed(units, settings.llm_line_window_chars, settings.llm_line_window_sentences)
    multiple = len(windows) > 1
    names = [name for name in (known_names or []) if name]

    characters = []
    relationships = []
    annotations: dict[int, LineAnnotation] = {}
    issues: list[dict] = []
    for position, (start, end) in enumerate(windows, start=1):
        window = units[start:end]
        # 提示按整章算再切片：归属句可能落在相邻窗口里，只看窗口内会漏
        hints = hints_for_names(units, names)[start:end]
        try:
            output = runner.run(
                system=CHAPTER_ANALYSIS_SYSTEM,
                user=chapter_analysis_user(
                    chapter_index,
                    title,
                    number_units(window, hints, start + 1),
                    chapter_text=content if multiple else "",
                    known_characters=names,
                ),
                model_cls=ChapterAnalysis,
                pass_name="A",
                book_id=book_id,
                chapter_index=chapter_index,
            )
        except LlmJsonError as exc:
            issues.append(
                {
                    "kind": "chapter_window_failed",
                    "reason": f"第 {start + 1}-{end} 句分析失败：{exc}",
                    "fallback": "这段按旁白 + 沿用上一句情绪",
                    "detail": {"sentences": len(window), "start": start + 1, "end": end},
                }
            )
            output = ChapterAnalysis()
        characters.extend(output.characters)
        relationships.extend(output.relationships)
        for annotation in output.lines:
            annotations[annotation.index] = annotation
        for card in output.characters:
            if card.name and card.name not in names:
                names.append(card.name)
        if on_window is not None:
            on_window(position, len(windows), f"第 {start + 1}-{end} 句")

    missing = [seq for seq in range(1, len(units) + 1) if seq not in annotations]
    if missing:
        issues.append(
            {
                "kind": "line_index_missing",
                "reason": f"{len(missing)} 句没有标注（如 {'、'.join(str(item) for item in missing[:5])}）",
                "fallback": "这些句子按归属句提示给说话人，情绪沿用上一句",
                "detail": {"missing": missing[:50]},
            }
        )
    return ChapterResult(
        analysis=ChapterAnalysis(
            characters=characters,
            relationships=relationships,
            lines=[annotations[seq] for seq in sorted(annotations)],
        ),
        issues=issues,
    )


def _is_narrator_name(name: str) -> bool:
    return (name or "").strip() in NARRATOR_NAMES


def materialize(
    *,
    chapter_index: int,
    units: list[Unit],
    annotations: list[LineAnnotation],
    characters_payload: dict,
    pronounce_table: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """把"角色名 + 情感"的标注落成行记录：名字 → role_id、拼缓存参数、算停顿与语速。"""
    index = characters_index(characters_payload)
    hints = hints_for_names(units, names_from_payload(characters_payload))
    by_index = {annotation.index: annotation for annotation in annotations}
    lines: list[dict] = []
    issues: list[dict] = []
    previous = None
    for offset, unit in enumerate(units):
        seq = offset + 1
        identifier = line_id(chapter_index, DEFAULT_SEGMENT, seq)
        kind = getattr(unit, "kind", "narration")
        hint_name = hints[offset]
        annotation = by_index.get(seq)
        if annotation is None:
            annotation = LineAnnotation(index=seq, speaker=hint_name or "旁白", emotion="继承")
        speaker_name = annotation.speaker
        speaker_id = resolve_speaker(characters_payload, speaker_name)
        if speaker_id is None:
            if not _is_narrator_name(speaker_name):
                issues.append(
                    {
                        "kind": "unknown_speaker",
                        "line": identifier,
                        "reason": f"未知说话人：{speaker_name}",
                        "fallback": "按旁白处理",
                    }
                )
            speaker_id = NARRATOR_ID
        # 对白被标成旁白是最常见也最刺眼的错误：归属句点名了谁，就按归属句纠正
        if kind == "dialogue" and speaker_id == NARRATOR_ID and hint_name:
            hinted = resolve_speaker(characters_payload, hint_name)
            if hinted and hinted != NARRATOR_ID:
                speaker_id = hinted
        row = derive_line(
            {
                "emotion": annotation.emotion,
                "intensity": annotation.intensity,
                "secondary": annotation.secondary,
                "secondary_weight": annotation.secondary_weight,
                "emotion_text": annotation.emotion_text,
                "delivery": annotation.delivery,
            },
            chapter_index=chapter_index,
            scene_index=DEFAULT_SEGMENT,
            seq=seq,
            sentence=_text_of(unit),
            kind=kind,
            speaker_id=speaker_id,
            speaker_name=role_name(characters_payload, speaker_id),
            addressee_id=None,
            addressee_name=None,
            character=index.get(speaker_id),
            relationship=None,
            pronounce_table=pronounce_table,
            previous_emotion=previous,
        )
        previous = row["emotion"]
        lines.append(row)
    return lines, issues
