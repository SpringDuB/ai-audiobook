"""逐句情感标注（Pass C）。

整章按字符数切成若干窗口，每个窗口单独请求一次 LLM：窗口内每一句都拿到
说话人 / 听话人 / 主情绪 + 副情绪 / 表演幅度 / 说话方式。句子编号沿用"本章第几句"，
所以行 id（c0001-s01-l003）跨窗口稳定，音频缓存不会因为窗口切分而失效。

这里没有场景切分：整章视为一段（segment=1），行记录里的 scene 字段只为兼容 id 形态保留。
"""

from ..llm.runner import LlmJsonError
from .characters import NARRATOR_ID, characters_index, relationships_index, resolve_speaker, role_name
from .derive import derive_line, line_id
from .models import NARRATOR_NAMES, LineAnnotation, PassCOutput
from .prompts import PASS_C_SYSTEM, pass_c_user

DEFAULT_SEGMENT = 1


def number_sentences(sentences: list[str], start: int = 1) -> str:
    """给句子编号（编号 = 本章第几句），让模型按编号作答。"""
    return "\n".join(f"{position}. {sentence}" for position, sentence in enumerate(sentences, start=start))


def windowed(sentences: list[str], max_chars: int, max_sentences: int = 80) -> list[tuple[int, int]]:
    """把整章句子切成窗口（左闭右开、不重叠）。

    同时限字符数和句数：模型要逐句输出记录，窗口太大输出会被 max_output_tokens 截断。
    """
    limit = max(1, int(max_chars))
    cap = max(1, int(max_sentences))
    windows: list[tuple[int, int]] = []
    start, size = 0, 0
    for index, sentence in enumerate(sentences):
        length = len(sentence)
        if size and (size + length > limit or index - start >= cap):
            windows.append((start, index))
            start, size = index, 0
        size += length
    if start < len(sentences):
        windows.append((start, len(sentences)))
    return windows


def _is_narrator_name(name: str) -> bool:
    return (name or "").strip() in NARRATOR_NAMES


def participants_in_window(sentences: list[str], characters_payload: dict) -> list[str]:
    """本窗口真正出现过的角色（含别名命中）+ 旁白：Pass C 只需要这些人。"""
    text = "".join(sentences)
    found: list[str] = []
    for character in characters_payload.get("characters", []):
        names = [character.get("name") or "", *(character.get("aliases") or [])]
        if any(name and name in text for name in names) and character["id"] not in found:
            found.append(character["id"])
    if NARRATOR_ID not in found:
        found.insert(0, NARRATOR_ID)
    return found


def build_context_block(characters_payload: dict, participants: list[str], relationships: dict) -> str:
    """角色卡 + 他们之间的关系：对白情绪的判据来自"谁对谁"。"""
    index = characters_index(characters_payload)
    lines = []
    for role_id in participants:
        character = index.get(role_id)
        if not character:
            continue
        lines.append(
            f"- {character['name']}（{character['gender']}，{character['age_group']}，"
            f"底色：{character['base_emotion']}，说话习惯：{character['speaking_style'] or '无'}）"
        )
    block = "本段角色：\n" + "\n".join(lines) if lines else "本段角色：无"
    relation_lines = []
    for (source, target), rel in sorted(relationships.items()):
        if source in participants and target in participants:
            relation_lines.append(
                f"- {index[source]['name']} → {index[target]['name']}：亲疏{rel['closeness']}、"
                f"尊卑{rel['hierarchy']}、敌意{rel['hostility']}、亲密{rel['intimacy']}"
            )
    if relation_lines:
        block += "\n\n本段人物关系：\n" + "\n".join(relation_lines)
    return block


def annotate_window(
    runner, *, settings, book_id, chapter_index, title, window, offset, characters_payload, relationships
):
    participants = participants_in_window(window, characters_payload)
    return runner.run(
        system=PASS_C_SYSTEM,
        user=pass_c_user(
            chapter_index,
            title,
            number_sentences(window, offset + 1),
            build_context_block(characters_payload, participants, relationships),
        ),
        model_cls=PassCOutput,
        pass_name="C",
        book_id=book_id,
        chapter_index=chapter_index,
    ).lines


def build_lines(
    *,
    chapter_index: int,
    sentences: list[str],
    start_seq: int,
    annotations,
    characters_payload: dict,
    relationships: dict,
    pronounce_table: dict[str, str],
    previous_emotion: dict | None = None,
):
    index = characters_index(characters_payload)
    by_index = {annotation.index: annotation for annotation in annotations}
    lines: list[dict] = []
    issues: list[dict] = []
    previous = previous_emotion
    for offset, sentence in enumerate(sentences):
        seq = start_seq + offset
        identifier = line_id(chapter_index, DEFAULT_SEGMENT, seq)
        annotation = by_index.get(seq)
        if annotation is None:
            annotation = LineAnnotation(index=seq, speaker="旁白", emotion="继承")
            issues.append(
                {
                    "kind": "line_index_missing",
                    "line": identifier,
                    "reason": f"模型未返回第 {seq} 句的标注",
                    "fallback": "旁白 + 沿用上一句情绪",
                }
            )
        speaker_id = resolve_speaker(characters_payload, annotation.speaker)
        if speaker_id is None:
            if not _is_narrator_name(annotation.speaker):
                issues.append(
                    {
                        "kind": "unknown_speaker",
                        "line": identifier,
                        "reason": f"未知说话人：{annotation.speaker}",
                        "fallback": "按旁白处理",
                    }
                )
            speaker_id = NARRATOR_ID
        addressee_id = resolve_speaker(characters_payload, annotation.addressee) if annotation.addressee else None
        relationship = relationships.get((speaker_id, addressee_id)) if addressee_id else None
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
            sentence=sentence,
            speaker_id=speaker_id,
            speaker_name=role_name(characters_payload, speaker_id),
            addressee_id=addressee_id,
            addressee_name=role_name(characters_payload, addressee_id) if addressee_id else None,
            character=index.get(speaker_id),
            relationship=relationship,
            pronounce_table=pronounce_table,
            previous_emotion=previous,
        )
        previous = row["emotion"]
        lines.append(row)
    return lines, issues


def process_chapter(
    runner,
    *,
    settings,
    book_id,
    chapter_index,
    title,
    sentences,
    characters_payload,
    pronounce_table,
    on_window=None,
):
    """整章逐句标注；某个窗口失败只降级这一段，不拖垮整章。"""
    relationships = relationships_index(characters_payload)
    all_lines: list[dict] = []
    issues: list[dict] = []
    windows = windowed(sentences, settings.llm_line_window_chars, settings.llm_line_window_sentences)
    total = len(windows)
    for position, (start, end) in enumerate(windows, start=1):
        window = sentences[start:end]
        if not window:
            continue
        try:
            annotations = annotate_window(
                runner,
                settings=settings,
                book_id=book_id,
                chapter_index=chapter_index,
                title=title,
                window=window,
                offset=start,
                characters_payload=characters_payload,
                relationships=relationships,
            )
        except LlmJsonError as exc:
            issues.append(
                {
                    "kind": "pass_c_failed",
                    "reason": str(exc),
                    "fallback": "该段按旁白 + 沿用上一句情绪",
                    "detail": {"sentences": len(window), "start": start + 1, "end": end},
                }
            )
            annotations = [
                LineAnnotation(index=start + 1 + offset, speaker="旁白", emotion="继承")
                for offset in range(len(window))
            ]
        window_lines, window_issues = build_lines(
            chapter_index=chapter_index,
            sentences=window,
            start_seq=start + 1,
            annotations=annotations,
            characters_payload=characters_payload,
            relationships=relationships,
            pronounce_table=pronounce_table,
            previous_emotion=all_lines[-1]["emotion"] if all_lines else None,
        )
        all_lines.extend(window_lines)
        issues.extend(window_issues)
        if on_window is not None:
            on_window(position, total, f"第 {start + 1}-{end} 句")
    return all_lines, issues
