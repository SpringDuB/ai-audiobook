"""分析链第四步：给每个角色写基础音色描述（纯大模型，不占显存）。

两条入口：
  - 整本：只补"还没有描述"的角色（断点续跑，重复点不会白烧 token），并且选角表
    也只给还没有音色（原型/描述）的角色分批定，已有的直接当已占用参照；
  - 单角色重写（payload.roles=[role_id]）：前端点"重新写一版"走这条，
    用户自己改的描述（description_source=manual）默认不动，除非 force。
    **不重跑全书选角表**：微调沿用角色已有的原型，换一版只按台词 + 已占用音色重设计。

流程：先跑「选角表」（给还没有原型的角色定音色原型、两两拉开），再按原型给每个
角色写描述 —— 这样同类角色不会撞成一个声音。选角表失败不影响主流程。

描述写进 casting.json；试听音频按需生成（API 的 preview 接口），不在这里占 GPU。
同一个文件里还有 `voice_preview` 任务：一次性把多个角色的试听音频生成出来
（已生成且描述没变的自动跳过），省得一个个点。
"""

import logging
import time

from .. import store
from ..analysis.design import (
    FALLBACK_SAMPLE,
    NARRATOR_FALLBACK_SAMPLE,
    build_cast_sheet,
    describe_many,
    pending_roles,
    role_briefs,
    save_description,
    top_avoid_rows,
    voice_hint,
)
from ..analysis.derive import derive_lang
from ..analysis.issues import record_issue
from ..analysis.roles import NARRATOR_ID
from ..analysis.voices import description_key, preview_path, preview_ready, write_preview_meta
from .common import require_llm
from ..worker import register

logger = logging.getLogger(__name__)

REWRITE_MODES = ("refine", "reroll")
# 用户换音色要求的长度上限（够写清年龄/音区/质地/气质，又不至于把提示词撑爆）
INSTRUCTION_CHARS = 300


def _scope(payload: dict) -> tuple[list[str], bool, str, str]:
    payload = payload or {}
    roles = [str(item) for item in payload.get("roles") or [] if item]
    mode = str(payload.get("mode") or "refine").strip().lower()
    if mode not in REWRITE_MODES:
        mode = "refine"
    # 用户自己写的换音色要求（"换一版音色"弹窗里填的那段话）
    instruction = str(payload.get("instruction") or "").strip()[:INSTRUCTION_CHARS]
    return roles, bool(payload.get("force")), mode, instruction


@register("voice_design")
def handle_voice_design(ctx, job) -> None:
    ctx.raise_if_cancelled(job)
    book_id = job.book_id
    characters = store.read_json(store.characters_path(ctx.settings, book_id))
    if not characters:
        raise RuntimeError("缺少 characters.json，请先跑 characters 任务")
    roles, force, mode, instruction = _scope(job.payload or {})

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

    casting = store.read_json(store.casting_path(ctx.settings, book_id), default={}) or {}
    known = casting.get("roles") or {}

    if roles:
        # 指定角色重写（换一版音色 / 微调）：只动这几个角色，不再重跑全书选角表。
        # 微调沿用已有原型（守住用户听过的音色）；换一版不受旧原型约束，
        # 只按"已占用音色"避让，免得和别的角色撞声。
        ctx.progress(job, 0, len(targets), f"重写 {len(targets)} 个角色的音色描述…")
        archetypes: dict[str, str] = {}
        if mode == "refine":
            for brief in targets:
                hint = voice_hint(known.get(brief["role_id"]))
                if hint:
                    archetypes[brief["name"]] = hint
        cast_issues: list[dict] = []
    else:
        # 选角表：只让有戏份、还没有原型的角色参与、分批做（600+ 角色的书一次全交给
        # 模型不现实）；每批都能看到前面已占用的原型，跨批也不会撞。
        ctx.progress(job, 0, len(targets), "先定选角表（已有原型的角色会跳过）…")
        archetypes, cast_issues = build_cast_sheet(
            require_llm(ctx),
            book_id=book_id,
            briefs=list(briefs.values()),
            known=known,
            batch_size=ctx.settings.cast_sheet_batch,
            max_roles=ctx.settings.cast_sheet_max_roles,
            min_lines=ctx.settings.cast_sheet_min_lines,
            on_progress=lambda done, total, message: ctx.progress(
                job, 0, len(targets), f"选角表 {done}/{total} 批：{message}"
            ),
            cancel_check=ctx.cancel_check(job),
        )
    for issue in cast_issues:
        record_issue(
            ctx.settings,
            book_id,
            issue["kind"],
            reason=issue["reason"],
            fallback=issue.get("fallback"),
            detail=issue.get("detail"),
        )

    # 微调模式：把上一版描述喂回去当锚点（用户听过的音色不该被一次重写换掉）
    previous_texts = {
        brief["role_id"]: str((known.get(brief["role_id"]) or {}).get("description") or "")
        for brief in targets
    } if mode == "refine" else {}
    previous_texts = {role_id: text for role_id, text in previous_texts.items() if text}

    # 出场少、没进选角表的角色：至少避开主角群已经占用的音色
    if roles:
        hints = {
            brief["name"]: voice_hint(known.get(brief["role_id"]))
            for brief in briefs.values()
        }
        target_names = {brief["name"] for brief in targets}
        avoid = top_avoid_rows(
            {name: text for name, text in hints.items() if text and name not in target_names},
            list(briefs.values()),
        )
    else:
        avoid = top_avoid_rows(archetypes, list(briefs.values()))
    directives = (
        {brief["role_id"]: instruction for brief in targets} if instruction else {}
    )

    ctx.raise_if_cancelled(job)
    results = describe_many(
        require_llm(ctx),
        book_id=book_id,
        briefs=targets,
        archetypes=archetypes,
        previous=previous_texts,
        directives=directives,
        avoid=avoid,
        concurrency=ctx.settings.llm_concurrency,
        on_progress=lambda done, total, name: ctx.progress(job, done, total, name),
        cancel_check=ctx.cancel_check(job),
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
        hint = archetypes.get(brief["name"], "")
        if roles and not hint:
            # 换一版没有新的选角表原型：把新描述截一段回写成"已占用"提示，
            # 免得旧原型在后续增量选角里继续代表这个角色的声音。
            hint = voice_hint({"description": plan["description"]})
        save_description(
            ctx.settings,
            book_id,
            role_id,
            description=plan["description"],
            sample=plan.get("sample") or "",
            source="llm",
            name=brief["name"],
            archetype=hint,
        )
        written += 1
    ctx.progress(
        job,
        len(targets),
        len(targets),
        f"完成：{written} 个角色已写描述，{failed} 个失败",
    )
    logger.info("音色描述任务完成：%d 个角色（失败 %d）", written, failed)


# ---------------------------------------------------------------- 一键生成全部试听


def _preview_targets(settings, book_id: str, payload: dict) -> tuple[list[tuple[str, dict]], int, int]:
    """哪些角色要生成试听：有描述、不是手工绑的库存音色、且（非 force）试听已过期。

    返回 (待生成, 已有描述的角色数, 跳过的角色数)。
    """
    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    roles = casting.get("roles") or {}
    wanted = [str(item) for item in (payload or {}).get("roles") or [] if item] or list(roles)
    force = bool((payload or {}).get("force"))
    targets: list[tuple[str, dict]] = []
    described = 0
    skipped = 0
    for role_id in wanted:
        role = roles.get(role_id) or {}
        description = str(role.get("description") or "").strip()
        if not description:
            continue  # 手工绑库存音色的角色没有描述：试听走音色库自己的 sample
        described += 1
        if not force and preview_ready(settings, book_id, role_id, description):
            skipped += 1
            continue
        targets.append((role_id, role))
    return targets, described, skipped


@register("voice_preview")
def handle_voice_preview(ctx, job) -> None:
    """把角色试听一次性生成出来（一个个点太麻烦）。已生成且描述没变的自动跳过。"""
    ctx.raise_if_cancelled(job)
    book_id = job.book_id
    payload = job.payload or {}
    targets, described, skipped = _preview_targets(ctx.settings, book_id, payload)
    if not described:
        raise RuntimeError("这本书还没有角色音色描述：先跑「音色描述」任务")
    if not targets:
        ctx.progress(job, 1, 1, f"{described} 个角色的试听都是最新的，无需生成")
        return
    if ctx.engine is None:
        raise RuntimeError("合成引擎不可用：先启动 TTS 服务")
    ctx.progress(job, 0, len(targets), f"待生成 {len(targets)} 个（跳过 {skipped} 个已最新的）")
    done = 0
    failed = 0
    for index, (role_id, role) in enumerate(targets, start=1):
        ctx.raise_if_cancelled(job)
        name = str(role.get("name") or role_id)
        description = str(role.get("description") or "").strip()
        sample = str(role.get("sample") or "").strip()
        if not sample:
            sample = NARRATOR_FALLBACK_SAMPLE if role_id == NARRATOR_ID else FALLBACK_SAMPLE
        try:
            result = ctx.engine.design_voice(
                instruct=description,
                text=sample,
                lang=derive_lang(sample),
                out_path=preview_path(ctx.settings, book_id, role_id),
            )
        except Exception as exc:  # noqa: BLE001 - 单个角色失败不能拖垮整批
            failed += 1
            logger.warning("角色 %s 的试听生成失败: %s", name, exc)
            record_issue(
                ctx.settings,
                book_id,
                "tts_line_failed",
                reason=f"{name} 的试听生成失败：{type(exc).__name__}: {exc}",
                fallback="这个角色跳过，其余角色继续；可单独点「试听」重试",
                detail={"role_id": role_id},
            )
        else:
            done += 1
            write_preview_meta(
                ctx.settings,
                book_id,
                role_id,
                {
                    "role_id": role_id,
                    "description_key": description_key(description),
                    "sample": sample,
                    "duration": round(float(result.duration), 3),
                    "updated_at": int(time.time() * 1000),
                },
            )
        ctx.progress(job, index, len(targets), f"{name}（{index}/{len(targets)}）")
    ctx.progress(
        job,
        len(targets),
        len(targets),
        f"完成：{done} 个试听已生成，{failed} 个失败，{skipped} 个已是最新",
    )
    logger.info("角色试听任务完成：%d 个生成、%d 个失败、%d 个跳过", done, failed, skipped)
