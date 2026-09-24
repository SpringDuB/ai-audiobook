from ..llm.runner import LlmJsonError
from .characters import NARRATOR_ID, characters_index, relationships_index, resolve_speaker, role_name
from .derive import derive_line, line_id
from .models import NARRATOR_NAMES, LineAnnotation, PassCOutput
from .prompts import PASS_C_SYSTEM, pass_c_user
from .scenes import number_sentences


def _is_narrator_name(name: str) -> bool:
    return (name or "").strip() in NARRATOR_NAMES


def build_context_block(characters_payload: dict, participants: list[str], relationships: dict) -> str:
    """只注入本场景参与者及其关系，控制 token 同时提高准确度。"""
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
    block = "本场景角色：\n" + "\n".join(lines)
    relation_lines = []
    for (source, target), rel in sorted(relationships.items()):
        if source in participants and target in participants:
            relation_lines.append(
                f"- {index[source]['name']} → {index[target]['name']}：亲疏{rel['closeness']}、"
                f"尊卑{rel['hierarchy']}、敌意{rel['hostility']}、亲密{rel['intimacy']}"
            )
    if relation_lines:
        block += "\n\n本场景人物关系：\n" + "\n".join(relation_lines)
    return block


def annotate_scene(runner, *, settings, book_id, chapter_index, scene, sentences, characters_payload, relationships):
    context = build_context_block(characters_payload, scene["participants"], relationships)
    return runner.run(
        system=PASS_C_SYSTEM,
        user=pass_c_user(chapter_index, scene, number_sentences(sentences), context),
        model_cls=PassCOutput,
        pass_name="C",
        book_id=book_id,
        chapter_index=chapter_index,
        scene_id=scene["id"],
    )


def build_lines(*, chapter_index, scene, sentences, annotations, characters_payload, relationships, pronounce_table):
    index = characters_index(characters_payload)
    by_index = {annotation.index: annotation for annotation in annotations}
    lines: list[dict] = []
    issues: list[dict] = []
    for seq, sentence in enumerate(sentences, start=1):
        identifier = line_id(chapter_index, scene["index"], seq)
        annotation = by_index.get(seq)
        if annotation is None:
            annotation = LineAnnotation(index=seq, speaker="旁白", emotion="继承")
            issues.append(
                {
                    "kind": "line_index_missing",
                    "line": identifier,
                    "reason": f"模型未返回第 {seq} 句的标注",
                    "fallback": "旁白 + 场景基调",
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
        lines.append(
            derive_line(
                {"emotion": annotation.emotion, "intensity": annotation.intensity, "delivery": annotation.delivery},
                chapter_index=chapter_index,
                scene_index=scene["index"],
                seq=seq,
                sentence=sentence,
                speaker_id=speaker_id,
                speaker_name=role_name(characters_payload, speaker_id),
                addressee_id=addressee_id,
                addressee_name=role_name(characters_payload, addressee_id) if addressee_id else None,
                scene_tone=scene["tone"],
                character=index.get(speaker_id),
                relationship=relationship,
                pronounce_table=pronounce_table,
                scene_switch=(seq == 1 and scene["index"] > 1),
            )
        )
    return lines, issues


def process_chapter(
    runner, *, settings, book_id, chapter_index, sentences, scenes_payload, characters_payload,
    pronounce_table, on_scene=None,
):
    relationships = relationships_index(characters_payload)
    all_lines: list[dict] = []
    issues: list[dict] = []
    scenes = scenes_payload["scenes"]
    total = len(scenes)
    for position, scene in enumerate(scenes, start=1):
        scene_sentences = sentences[scene["start_line"] - 1: scene["end_line"]]
        if not scene_sentences:
            continue
        try:
            annotations = annotate_scene(
                runner, settings=settings, book_id=book_id, chapter_index=chapter_index,
                scene=scene, sentences=scene_sentences, characters_payload=characters_payload,
                relationships=relationships,
            ).lines
        except LlmJsonError as exc:
            issues.append(
                {
                    "kind": "pass_c_failed",
                    "scene": scene["id"],
                    "reason": str(exc),
                    "fallback": "整场景按旁白 + 场景基调",
                    "detail": {"sentences": len(scene_sentences)},
                }
            )
            annotations = [
                LineAnnotation(index=seq, speaker="旁白", emotion="继承")
                for seq in range(1, len(scene_sentences) + 1)
            ]
        scene_lines, scene_issues = build_lines(
            chapter_index=chapter_index, scene=scene, sentences=scene_sentences,
            annotations=annotations, characters_payload=characters_payload,
            relationships=relationships, pronounce_table=pronounce_table,
        )
        all_lines.extend(scene_lines)
        issues.extend(scene_issues)
        if on_scene is not None:
            on_scene(position, total, scene["id"])
    return all_lines, issues
