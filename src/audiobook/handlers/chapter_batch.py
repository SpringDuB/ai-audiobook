"""勾选多章的批量分析：一个 job 内部按大模型并发同时提取，和整本 characters 同一模式。

和 characters（整本）的区别只有三点：
- 范围：只跑勾选的章节，其它章的提取结果一个字都不碰；
- 角色表：新称呼并进已有角色表（extend_characters），不重建、不覆盖老角色；
- 收尾：给勾选章入队 lines（用最终角色表物化 + 作废过期成品），由它们触达选角链。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs, store
from ..analysis.extract import ChapterExtraction, dump_extraction, extract_chapter, load_extraction
from ..analysis.issues import record_issue
from ..analysis.merge import extend_characters
from ..analysis.roles import names_from_payload
from ..worker import JobCancelled, register
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
    # 默认跳过已经分析好的章节（断点续跑）；只有显式 force 才全部重提
    force = bool((job.payload or {}).get("force"))

    results: list[tuple[int, list]] = []
    reused: list[int] = []
    pending: list[dict] = []
    for chapter in chapters:
        index = int(chapter["index"])
        cached = None if force else store.read_json(store.extract_path(ctx.settings, book_id, index))
        lines = load_extraction(cached) if cached else []
        if lines:
            # 这一章已经分析过：一个 LLM 请求都不发，只把旧结果并进角色表
            results.append((index, lines))
            reused.append(index)
            continue
        pending.append(chapter)

    done = len(reused)
    # 正在提取的章节号：前端给这些章画"正在分析台词"的沙漏（同一时刻可能有多章）
    inflight: set[int] = set()
    # 已经有提取结果的章号（含本轮复用的）：前端据此撤掉"等待分析"
    finished: set[int] = set(reused)
    inflight_lock = threading.Lock()
    if reused:
        logger.info("跳过 %d 章已分析好的章节：%s", len(reused), reused[:20])
        ctx.progress(
            job, done, len(chapters), f"跳过 {len(reused)} 章已分析", extra={"chapters_inflight": []}
        )

    def extract_one(chapter) -> ChapterExtraction:
        index = int(chapter["index"])
        with inflight_lock:
            inflight.add(index)
            snapshot = sorted(inflight)
            done_snapshot = sorted(finished)
        ctx.progress(
            job,
            done,
            len(chapters),
            f"第 {index} 章",
            extra={"chapters_inflight": snapshot, "chapters_done": done_snapshot},
        )
        try:
            return extract_chapter(
                runner,
                book_id=book_id,
                chapter_index=index,
                title=chapter.get("title") or f"第{index}章",
                content=chapter["content"],
                window_chars=ctx.settings.llm_line_window_chars,
                known_names=known_names,
            )
        finally:
            with inflight_lock:
                inflight.discard(index)

    cancelled = False
    if pending:
        max_workers = max(1, min(len(pending), int(ctx.settings.llm_concurrency)))
        # 不用 with：取消时要立刻返回，不能让在跑的请求把任务拖到跑完
        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {pool.submit(extract_one, chapter): chapter for chapter in pending}
            for future in as_completed(futures):
                if ctx.cancelled(job):
                    cancelled = True
                    break
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
                    with inflight_lock:
                        finished.add(int(chapter["index"]))
                    done += 1
                    ctx.progress(
                        job,
                        done,
                        len(chapters),
                        f"第 {chapter['index']} 章",
                        extra={
                            "chapters_inflight": sorted(inflight),
                            "chapters_done": sorted(finished),
                        },
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    if cancelled:
        # 已提取的章都写在盘上了，下次再点会跳过它们
        raise JobCancelled(f"已取消：{done}/{len(chapters)} 章有提取结果，下次分析会自动跳过")
    if not results:
        raise RuntimeError("勾选的章节全部提取失败，请检查 LLM 端点")

    results.sort(key=lambda item: item[0])
    ctx.raise_if_cancelled(job)
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
