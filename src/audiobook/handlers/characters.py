"""分析链第一步：逐章提取（大模型直出「句子 + 说话人 + 情绪」），再做全书角色整合。

提取和整合都是 LLM：提取按章并发，整合在全部章提取完后跑一次。
角色表落盘后为每一章入队 lines（物化落盘）；单章失败不拖垮全书，但会写异常清单。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs, store
from ..analysis.extract import (
    ChapterExtraction,
    dump_extraction,
    extract_chapter,
    is_legacy_extraction,
    load_extraction,
)
from ..analysis.issues import record_issue
from ..analysis.merge import merge_roles, role_entries
from ..analysis.roles import names_from_payload
from ..worker import JobCancelled, register
from .common import require_llm
from .lines import materialize_chapter

logger = logging.getLogger(__name__)


@register("characters")
def handle_characters(ctx, job) -> None:
    runner = require_llm(ctx)
    book_id = job.book_id
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        raise RuntimeError("没有分章结果，请先跑 chapter_split")
    # 整本重跑默认跳过已经分析好的章节（断点续跑）：只有显式 force 才全部重提
    force = bool((job.payload or {}).get("force"))

    # 重跑时拿旧角色表里的名字当参照，保证跨章叫法一致（不是让它照抄）
    existing = store.read_json(store.characters_path(ctx.settings, book_id), default={}) or {}
    known_names = names_from_payload(existing)

    results: list[tuple[int, list]] = []
    reused: list[int] = []
    pending: list[dict] = []
    for chapter in chapters:
        index = int(chapter["index"])
        cached = None if force else store.read_json(store.extract_path(ctx.settings, book_id, index))
        if is_legacy_extraction(cached):
            # 老结果没有逐句表演描述（换 Qwen3-TTS 之前的格式）：重新提取这一章
            cached = None
        lines = load_extraction(cached) if cached else []
        if lines:
            # 这一章已经分析过：一个 LLM 请求都不发，直接拿旧结果参与整合
            results.append((index, lines))
            reused.append(index)
            continue
        pending.append(chapter)

    done = len(reused)
    # 正在提取的章节号：写进任务进度，前端给这些章画"正在分析台词"的沙漏
    inflight: set[int] = set()
    # 已经有提取结果的章号（含本轮复用的）：前端据此撤掉"等待分析"（已分析完的不再显示状态）
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
                cancel_check=check,
            )
        finally:
            with inflight_lock:
                inflight.discard(index)

    cancelled = False
    if pending:
        check = ctx.cancel_check(job)
        # 不用 with：取消时要立刻返回，不能让在跑的请求把任务拖到跑完
        pool = ThreadPoolExecutor(max_workers=max(1, min(len(pending), int(ctx.settings.llm_concurrency))))
        try:
            futures = {pool.submit(extract_one, chapter): chapter for chapter in pending}
            for future in as_completed(futures):
                if ctx.cancelled(job):
                    cancelled = True
                    break
                chapter = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # 单章失败不拖垮全书，但必须可见
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
                    # 边提取边落行：前端不用等全书整合跑完，一章算完就能看到角色文本
                    try:
                        materialize_chapter(
                            ctx,
                            book_id=book_id,
                            chapter_index=chapter["index"],
                            spoken=result.lines,
                            allow_llm=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - 提前落行失败不影响整本分析
                        logger.warning("第 %s 章提前落行失败: %s", chapter["index"], exc)
                        record_issue(
                            ctx.settings,
                            book_id,
                            "chapter_materialize_failed",
                            reason=f"{type(exc).__name__}: {exc}",
                            chapter=chapter["index"],
                            fallback="整本提取结束后由 lines 任务重新落行",
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
        # 已提取的章都写在盘上了，下次点「分析全本台词」会跳过它们
        raise JobCancelled(f"已取消：{done}/{len(chapters)} 章有提取结果，下次分析会自动跳过")
    if not results:
        raise RuntimeError("整章提取全部失败，请检查 LLM 端点")

    results.sort(key=lambda item: item[0])
    ctx.raise_if_cancelled(job)
    payload, issues = merge_roles(
        runner,
        book_id=book_id,
        entries=role_entries(results),
        known=known_names,
        cancel_check=ctx.cancel_check(job),
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
    store.atomic_replace_json(store.characters_path(ctx.settings, book_id), payload)
    for chapter in chapters:
        jobs.enqueue(ctx.conn, "lines", book_id, chapter["index"])
    ctx.conn.execute("UPDATE books SET status='analyzed' WHERE id=?", (book_id,))
    ctx.progress(job, len(chapters), len(chapters), f"角色 {len(payload['characters'])} 个")
