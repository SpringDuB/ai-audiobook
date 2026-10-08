"""分析链第三步：角色音色登记（角色 → casting.json），并把缺描述的角色交给下一步。

这一步本身不调大模型：
  - 把 characters.json 里的角色登记成 casting.json 的条目（voice_source=design）；
  - 用户手工绑过库存音色的角色保持 voice_source=library 不动；
  - 登记完把"还没有基础音色描述"的角色交给 voice_design 任务去写描述。
"""

import logging

from .. import jobs, store
from ..analysis.casting import load_voice_library, register_roles
from ..analysis.design import pending_roles, role_briefs
from ..worker import register

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
    scope = _scope_chapters(job)
    indexes = scope or [int(chapter["index"]) for chapter in chapters]
    lines_by_chapter = {
        int(index): store.read_jsonl(store.lines_path(ctx.settings, book_id, int(index))) for index in indexes
    }
    previous = store.read_json(store.casting_path(ctx.settings, book_id), default={}) or {}
    casting = register_roles(
        book_id=book_id,
        characters=characters,
        previous=previous,
        voices=load_voice_library(ctx.settings),
    )
    ctx.raise_if_cancelled(job)
    store.atomic_replace_json(store.casting_path(ctx.settings, book_id), casting)

    # 还没有基础音色描述的角色 → 交给 voice_design。
    # 这里刻意不带 roles 参数：任务本身按"谁缺描述就补谁"来跑，
    # 反复入队（多章各自登记一次）会被幂等去重，也不会漏掉后出现的角色。
    briefs = role_briefs(characters, lines_by_chapter)
    pending = pending_roles(characters, briefs, ctx.settings, book_id)
    if pending:
        jobs.enqueue(ctx.conn, "voice_design", book_id)
    logger.info(
        "选角登记：%d 个角色，%d 个待写音色描述",
        len(casting["roles"]),
        len(pending),
    )
    ctx.progress(job, len(casting["roles"]), len(casting["roles"]), f"{len(casting['roles'])} 个角色")
