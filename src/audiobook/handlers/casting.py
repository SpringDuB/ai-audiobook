"""分析链第三步：为每个角色推荐 1–3 个音色（大模型推荐，本地只校验与兜底）。

两种范围：
- 整本（payload 没有 chapters）：全量重算，手选保留；
- 单章/多章（payload.chapters）：只给还没有推荐的角色补推荐，已有推荐与手选不动。
  单章「分析本章台词」会走这条路，保证新冒出来的角色当场就有推荐音色可挑。
"""

import logging

from .. import store
from ..analysis.casting import (
    build_casting,
    fill_casting_for_characters,
    load_voice_library,
    samples_by_role,
)
from ..analysis.issues import record_issue
from ..worker import register
from .common import require_llm

logger = logging.getLogger(__name__)


def _scope_chapters(job) -> list[int]:
    raw = (job.payload or {}).get("chapters") or []
    try:
        return sorted({int(index) for index in raw})
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"选角的 chapters 参数不合法: {raw!r}") from exc


@register("casting")
def handle_casting(ctx, job) -> None:
    ctx.raise_if_cancelled(job)
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
    scope = _scope_chapters(job)
    if scope:
        # 增量：只为还没有推荐的角色补（单章分析后立刻有音色可挑）
        casting, issues = fill_casting_for_characters(
            require_llm(ctx) if voices else None,
            book_id=book_id,
            characters=characters,
            samples=samples_by_role(lines_by_chapter),
            voices=voices,
            previous=previous,
            concurrency=ctx.settings.llm_concurrency,
            on_progress=lambda done, total, name: ctx.progress(job, done, total, name),
        )
    else:
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
    # 算完到落盘之间再确认一次：取消掉的任务不该把半成品写进 casting.json
    ctx.raise_if_cancelled(job)
    store.atomic_replace_json(store.casting_path(ctx.settings, book_id), casting)
    # 选角是分析链的最后一步：到此为止，合成由用户点「生成有声书」触发
    logger.info("选角完成：%d 个角色（合成链等「生成有声书」）", len(casting["roles"]))
    ctx.progress(job, len(casting["roles"]), len(casting["roles"]), f"{len(casting['roles'])} 个角色")
