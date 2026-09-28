"""分析链第三步：为每个角色推荐 1–3 个音色（大模型推荐，本地只校验与兜底）。"""

import logging

from .. import store
from ..analysis.casting import build_casting, load_voice_library, samples_by_role
from ..analysis.issues import record_issue
from ..worker import register
from .characters import require_llm

logger = logging.getLogger(__name__)


@register("casting")
def handle_casting(ctx, job) -> None:
    book_id = job.book_id
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    lines_by_chapter = {
        int(chapter["index"]): store.read_jsonl(store.lines_path(ctx.settings, book_id, int(chapter["index"])))
        for chapter in chapters
    }
    voices = load_voice_library(ctx.settings)
    previous = store.read_json(store.casting_path(ctx.settings, book_id), default={}) or {}
    casting, issues = build_casting(
        require_llm(ctx) if voices else None,
        book_id=book_id,
        characters=characters,
        samples=samples_by_role(lines_by_chapter),
        voices=voices,
        previous=previous,
        concurrency=ctx.settings.llm_concurrency,
        on_progress=lambda done, total, name: ctx.progress(job, done, total, name),
    )
    for issue in issues:
        record_issue(
            ctx.settings,
            book_id,
            issue["kind"],
            reason=issue["reason"],
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )
    store.atomic_replace_json(store.casting_path(ctx.settings, book_id), casting)
    # 选角是分析链的最后一步：到此为止，合成由用户点「生成有声书」触发
    logger.info("选角完成：%d 个角色（合成链等「生成有声书」）", len(casting["roles"]))
    ctx.progress(job, len(casting["roles"]), len(casting["roles"]), f"{len(casting['roles'])} 个角色")
