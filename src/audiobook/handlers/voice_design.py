"""分析链第四步：给每个角色写基础音色描述（纯大模型，不占显存）。

两条入口：
  - 整本：只补"还没有描述"的角色（断点续跑，重复点不会白烧 token）；
  - 单角色重写（payload.roles=[role_id]）：前端点"重新写一版"走这条，
    用户自己改的描述（description_source=manual）默认不动，除非 force。

描述写进 casting.json；试听音频按需生成（API 的 preview 接口），不在这里占 GPU。
"""

import logging

from .. import store
from ..analysis.design import describe_many, pending_roles, role_briefs, save_description
from ..analysis.issues import record_issue
from .common import require_llm
from ..worker import register

logger = logging.getLogger(__name__)


def _scope(payload: dict) -> tuple[list[str], bool]:
    roles = [str(item) for item in (payload or {}).get("roles") or [] if item]
    return roles, bool((payload or {}).get("force"))


@register("voice_design")
def handle_voice_design(ctx, job) -> None:
    ctx.raise_if_cancelled(job)
    book_id = job.book_id
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    roles, force = _scope(job.payload or {})

    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    lines_by_chapter = {
        int(chapter["index"]): store.read_jsonl(store.lines_path(ctx.settings, book_id, int(chapter["index"])))
        for chapter in chapters
    }
    briefs = role_briefs(characters, lines_by_chapter)
    targets = pending_roles(
        characters, briefs, ctx.settings, book_id, roles=roles or None, force=force or bool(roles)
    )
    if not targets:
        ctx.progress(job, 1, 1, "所有角色都已有音色描述")
        return

    results = describe_many(
        require_llm(ctx),
        book_id=book_id,
        briefs=targets,
        concurrency=ctx.settings.llm_concurrency,
        on_progress=lambda done, total, name: ctx.progress(job, done, total, name),
    )

    written = 0
    failed = 0
    for brief in targets:
        role_id = brief["role_id"]
        plan, issues = results.get(role_id, ({}, []))
        for issue in issues:
            record_issue(
                ctx.settings,
                book_id,
                issue["kind"],
                reason=issue["reason"],
                fallback=issue.get("fallback"),
                detail=issue.get("detail"),
            )
        if not plan:
            # 描述没生成出来：沿用上一次的（有就继续用，没有就等下次重跑）
            failed += 1
            previous = (store.read_json(store.casting_path(ctx.settings, book_id), default={}) or {}).get(
                "roles", {}
            ).get(role_id) or {}
            if previous.get("description"):
                logger.info("%s 的描述生成失败，沿用上一次的描述", brief["name"])
            continue
        ctx.raise_if_cancelled(job)
        save_description(
            ctx.settings,
            book_id,
            role_id,
            description=plan["description"],
            sample=plan.get("sample") or "",
            source="llm",
            name=brief["name"],
        )
        written += 1
    ctx.progress(
        job,
        len(targets),
        len(targets),
        f"完成：{written} 个角色已写描述，{failed} 个失败",
    )
    logger.info("音色描述任务完成：%d 个角色（失败 %d）", written, failed)
