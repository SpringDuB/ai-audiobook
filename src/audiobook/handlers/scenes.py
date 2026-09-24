from .. import jobs, store
from ..analysis.issues import record_issue
from ..analysis.scenes import split_scenes
from ..text.split_chapters import split_sentences
from ..worker import register
from .characters import require_llm


@register("scenes")
def handle_scenes(ctx, job) -> None:
    runner = require_llm(ctx)
    book_id, chapter_index = job.book_id, job.chapter_index
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    chapter = next((item for item in chapters if item["index"] == chapter_index), None)
    if chapter is None:
        raise RuntimeError(f"chapters.json 里没有第 {chapter_index} 章")
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    sentences = split_sentences(chapter["content"])
    if not sentences:
        raise RuntimeError(f"第 {chapter_index} 章没有可分析的句子")
    ctx.progress(job, 0, len(sentences), "场景切分中")
    payload, issues = split_scenes(
        runner,
        settings=ctx.settings,
        book_id=book_id,
        chapter_index=chapter_index,
        title=chapter["title"],
        sentences=sentences,
        characters_payload=characters,
    )
    for issue in issues:
        record_issue(
            ctx.settings,
            book_id,
            issue["kind"],
            reason=issue["reason"],
            chapter=chapter_index,
            scene=issue.get("scene"),
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    store.atomic_replace_json(store.scenes_path(ctx.settings, book_id, chapter_index), payload)
    jobs.enqueue(ctx.conn, "lines", book_id, chapter_index)
    ctx.progress(job, len(payload["scenes"]), len(payload["scenes"]), f"{len(payload['scenes'])} 个场景")
