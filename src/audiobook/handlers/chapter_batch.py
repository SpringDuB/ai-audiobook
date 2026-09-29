"""勾选多章的批量分析：一个 job 内部按大模型并发同时提取，和整本 characters 同一模式。

和 characters（整本）的区别只有三点：
- 范围：只跑勾选的章节，其它章的提取结果一个字都不碰；
- 角色表：新称呼并进已有角色表（extend_characters），不重建、不覆盖老角色；
- 收尾：给勾选章入队 lines（用最终角色表物化 + 作废过期成品），由它们触达选角链。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs, store
from ..analysis.extract import dump_extraction, extract_chapter
from ..analysis.issues import record_issue
from ..analysis.merge import extend_characters
from ..analysis.roles import names_from_payload
from ..worker import register
from .common import require_llm
from .lines import materialize_chapter

logger = logging.getLogger(__name__)


def _picked_chapters(job) -> list[int]:
    raw = (job.payload or {}).get("chapters") or []
    try:
        picked = sorted({int(index) for index in raw})
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"批量分析的 chapters 参数不合法: {raw!r}") from exc
    if not picked:
        raise RuntimeError("批量分析缺少 chapters 参数")
    return picked


@register("chapters")
def handle_chapters(ctx, job) -> None:
    runner = require_llm(ctx)
    book_id = job.book_id
    picked = _picked_chapters(job)
    all_chapters = (
        store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}
    ).get("chapters") or []
    if not all_chapters:
        raise RuntimeError("没有分章结果，请先跑 chapter_split")
    by_index = {int(chapter["index"]): chapter for chapter in all_chapters}
    unknown = [index for index in picked if index not in by_index]
    if unknown:
        raise RuntimeError(f"chapters.json 里没有这些章: {unknown}")
    chapters = [by_index[index] for index in picked]

    existing = store.read_json(store.characters_path(ctx.settings, book_id), default={}) or {}
    known_names = names_from_payload(existing)

    results: list[tuple[int, list]] = []
    done = 0
    max_workers = max(1, min(len(chapters), int(ctx.settings.llm_concurrency)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                extract_chapter,
                runner,
                book_id=book_id,
                chapter_index=chapter["index"],
                title=chapter.get("title") or f"第{chapter['index']}章",
                content=chapter["content"],
                window_chars=ctx.settings.llm_line_window_chars,
                known_names=known_names,
            ): chapter
            for chapter in chapters
        }
        for future in as_completed(futures):
            chapter = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - 单章失败不拖垮其它勾选章
                logger.warning("第 %s 章提取失败: %s", chapter["index"], exc)
                record_issue(
                    ctx.settings,
                    book_id,
                    "chapter_extract_failed",
                    reason=f"{type(exc).__name__}: {exc}",
                    chapter=chapter["index"],
                    fallback="本章不参与角色整合，行数据留空（可单独重跑本章）",
                    detail={"title": chapter.get("title")},
                )
            else:
                store.atomic_replace_json(
                    store.extract_path(ctx.settings, book_id, chapter["index"]),
                    dump_extraction(result),
                )
                for issue in result.issues:
                    record_issue(
                        ctx.settings,
                        book_id,
                        issue["kind"],
                        reason=issue["reason"],
                        chapter=chapter["index"],
                        fallback=issue.get("fallback"),
                        detail=issue.get("detail"),
                    )
                # 边提取边落行：前端不用等整批跑完，一章算完就能看到角色文本
                try:
                    materialize_chapter(
                        ctx,
                        book_id=book_id,
                        chapter_index=chapter["index"],
                        spoken=result.lines,
                        allow_llm=False,
                    )
                except Exception as exc:  # noqa: BLE001 - 提前落行失败不影响整批
                    logger.warning("第 %s 章提前落行失败: %s", chapter["index"], exc)
                    record_issue(
                        ctx.settings,
                        book_id,
                        "chapter_materialize_failed",
                        reason=f"{type(exc).__name__}: {exc}",
                        chapter=chapter["index"],
                        fallback="角色整合结束后由 lines 任务重新落行",
                    )
                results.append((chapter["index"], result.lines))
            finally:
                done += 1
                ctx.progress(job, done, len(chapters), f"第 {chapter['index']} 章")
    if not results:
        raise RuntimeError("勾选的章节全部提取失败，请检查 LLM 端点")

    results.sort(key=lambda item: item[0])
    # 整批只调一次整合：LLM 只看到"新称呼"，老角色表原样保留
    characters, issues = extend_characters(
        runner, book_id=book_id, payload=existing, extractions=results
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
    store.atomic_replace_json(store.characters_path(ctx.settings, book_id), characters)
    # 用最终角色表再物化一次：role_id 可能与临时表不同；顺带作废过期成品、触达选角
    for chapter in chapters:
        jobs.enqueue(ctx.conn, "lines", book_id, chapter["index"])
    ctx.progress(job, len(chapters), len(chapters), f"角色 {len(characters['characters'])} 个")
