from ..llm.runner import LlmJsonError
from .models import EMOTIONS, PassBOutput, SceneSpan, clamp01
from .prompts import PASS_B_SYSTEM, pass_b_user


def scene_id(chapter_index: int, scene_index: int) -> str:
    return f"c{chapter_index:04d}-s{scene_index:02d}"


def number_sentences(sentences: list[str], start: int = 1) -> str:
    return "\n".join(f"{position}. {sentence}" for position, sentence in enumerate(sentences, start=start))


def windowed(sentences: list[str], max_chars: int) -> list[tuple[int, int]]:
    limit = max(1, int(max_chars))
    windows: list[tuple[int, int]] = []
    start, size = 0, 0
    for index, sentence in enumerate(sentences):
        length = len(sentence)
        if size and size + length > limit:
            windows.append((start, index))
            start, size = index, 0
        size += length
    if start < len(sentences):
        windows.append((start, len(sentences)))
    return windows


def _normalize_tone(tone: str) -> str:
    return tone if tone in EMOTIONS else "平静"


def _find(sentences: list[str], hint: str, start: int, end: int) -> int | None:
    hint = (hint or "").strip()
    if not hint:
        return None
    for index in range(start, end):
        if sentences[index].strip().startswith(hint[:20]):
            return index
    for index in range(start, end):
        if hint[:12] and hint[:12] in sentences[index]:
            return index
    return None


def _even_split(start: int, end: int, count: int) -> list[tuple[int, int]]:
    total = end - start
    base, extra = divmod(total, count)
    bounds = []
    cursor = start
    for position in range(count):
        size = base + (1 if position < extra else 0)
        bounds.append((cursor, cursor + size - 1))
        cursor += size
    return bounds


def _scene(index, span, start_line, end_line, name_to_id, fallback_title="") -> dict:
    participants: list[str] = []
    for name in span.participants:
        role_id = name_to_id.get((name or "").strip())
        if role_id and role_id not in participants:
            participants.append(role_id)
    return {
        "id": None,
        "index": index,
        "title": span.title or fallback_title,
        "summary": span.summary,
        "participants": participants,
        "tone": {"dominant": _normalize_tone(span.tone), "intensity": round(clamp01(span.tone_intensity), 3)},
        "start_line": start_line,
        "end_line": end_line,
    }


def locate_spans(sentences, spans, offset, name_to_id) -> tuple[list[dict], dict | None]:
    """在窗口内定位场景边界；`offset` 是窗口首句在全章中的下标（用于生成相对原书行号）。"""
    total = len(sentences)
    spans = list(spans)
    if len(spans) <= 1:
        span = spans[0] if spans else SceneSpan(index=1)
        return [_scene(1, span, offset + 1, offset + total, name_to_id)], None

    located: list[tuple[SceneSpan, int, int]] = []
    cursor = 0
    failed = False
    for span in spans:
        start = _find(sentences, span.starts_with, cursor, total)
        if start is None:
            failed = True
            break
        end = _find(sentences, span.ends_with, start, total)
        if end is None or end < start:
            failed = True
            break
        located.append((span, start, end))
        cursor = end + 1

    if failed:
        bounds = _even_split(0, total, len(spans))
        scenes = [
            _scene(position + 1, span, offset + start + 1, offset + end + 1, name_to_id)
            for position, (span, (start, end)) in enumerate(zip(spans, bounds))
        ]
        issue = {
            "kind": "scene_hint_not_found",
            "reason": "模型返回的场景边界句子在原文中定位失败，已按场景数均匀切分",
            "fallback": "均匀切分",
            "detail": {"sentences": len(sentences), "spans": len(spans)},
        }
        return scenes, issue

    scenes = [
        _scene(position + 1, span, offset + start + 1, offset + end + 1, name_to_id)
        for position, (span, start, end) in enumerate(located)
    ]
    return scenes, None


def dominant_tone(characters_payload: dict, participants: list[str]) -> dict:
    index = {character["id"]: character for character in characters_payload.get("characters", [])}
    counts: dict[str, int] = {}
    intensities: list[float] = []
    for role_id in participants:
        character = index.get(role_id)
        if not character:
            continue
        counts[character["base_emotion"]] = counts.get(character["base_emotion"], 0) + 1
        intensities.append(float(character["base_intensity"]))
    if not counts:
        return {"dominant": "平静", "intensity": 0.3}
    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    return {"dominant": dominant, "intensity": round(sum(intensities) / len(intensities), 3)}


def _name_index(characters_payload: dict) -> dict[str, str]:
    index: dict[str, str] = {}
    for character in characters_payload.get("characters", []):
        index[character["name"]] = character["id"]
        for alias in character["aliases"]:
            index.setdefault(alias, character["id"])
    return index


def split_scenes(runner, *, settings, book_id, chapter_index, title, sentences, characters_payload):
    index = _name_index(characters_payload)
    names = [character["name"] for character in characters_payload.get("characters", [])]
    scenes: list[dict] = []
    issues: list[dict] = []

    for start, end in windowed(sentences, settings.llm_scene_window_chars):
        window = sentences[start:end]
        if len(window) <= 1:
            span = SceneSpan(index=1, title=title, participants=names, starts_with=window[0] if window else "")
            located, issue = locate_spans(window, [span], offset=start, name_to_id=index)
        else:
            try:
                out = runner.run(
                    system=PASS_B_SYSTEM,
                    user=pass_b_user(chapter_index, title, names, number_sentences(window, start + 1)),
                    model_cls=PassBOutput,
                    pass_name="B",
                    book_id=book_id,
                    chapter_index=chapter_index,
                )
                if not out.scenes:
                    raise LlmJsonError("场景列表为空", 1, "scenes 为空")
                located, issue = locate_spans(window, out.scenes, offset=start, name_to_id=index)
            except LlmJsonError as exc:
                participant_ids = [index[name] for name in names if name in index]
                fallback_tone = dominant_tone(characters_payload, participant_ids)
                located = [
                    {
                        "id": None,
                        "index": 1,
                        "title": title,
                        "summary": "",
                        "participants": participant_ids,
                        "tone": fallback_tone,
                        "start_line": start + 1,
                        "end_line": end,
                    }
                ]
                issue = {
                    "kind": "pass_b_failed",
                    "reason": str(exc),
                    "fallback": "整窗口 1 个场景 + 角色底色均值",
                    "detail": {"sentences": len(window)},
                }
        scenes.extend(located)
        if issue:
            issues.append(issue)

    for position, scene in enumerate(scenes, start=1):
        scene["index"] = position
        scene["id"] = scene_id(chapter_index, position)
    payload = {
        "book_id": book_id,
        "chapter_index": chapter_index,
        "title": title,
        "sentence_count": len(sentences),
        "scenes": scenes,
    }
    return payload, issues
