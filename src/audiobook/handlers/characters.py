import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs, store
from ..analysis.characters import aggregate_characters, extract_chapter
from ..analysis.issues import record_issue
from ..worker import register

logger = logging.getLogger(__name__)


def require_llm(ctx):
    if ctx.llm is None:
        raise RuntimeError("worker 未配置 LLM：请设置 AB_LLM_BASE_URL 后重启 worker")
    return ctx.llm


@register("characters")
def handle_characters(ctx, job) -> None:
    runner = require_llm(ctx)
    book_id = job.book_id
    chapters = (store.read_json(store.chapters_path(ctx.settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        raise RuntimeError("没有分章结果，请先跑 chapter_split")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, ctx.settings.llm_concurrency)) as pool:
        futures = {
            pool.submit(
                extract_chapter,
                runner,
                settings=ctx.settings,
                book_id=book_id,
                chapter_index=chapter["index"],
                title=chapter["title"],
                content=chapter["content"],
            ): chapter
            for chapter in chapters
        }
        for future in as_completed(futures):
            chapter = futures[future]
            try:
                results.append((chapter["index"], future.result()))
            except Exception as exc:  # 单章失败不拖垮全书，但必须可见
                logger.warning("第 %s 章 Pass A 失败: %s", chapter["index"], exc)
                record_issue(
                    ctx.settings,
                    book_id,
                    "pass_a_chapter_skipped",
                    reason=f"{type(exc).__name__}: {exc}",
                    chapter=chapter["index"],
                    fallback="本章不参与角色聚合",
                    detail={"title": chapter["title"]},
                )
            finally:
                done += 1
                ctx.progress(job, done, len(chapters), f"第 {chapter['index']} 章")
    if not results:
        raise RuntimeError("Pass A 全部章节失败，请检查 LLM 端点")

    results.sort(key=lambda item: item[0])
    aggregate = aggregate_characters(results)
    store.atomic_replace_json(
        store.characters_path(ctx.settings, book_id),
        {"book_id": book_id, "generated_at": int(time.time() * 1000), **aggregate},
    )
    for chapter in chapters:
        jobs.enqueue(ctx.conn, "lines", book_id, chapter["index"])
    ctx.conn.execute("UPDATE books SET status='analyzed' WHERE id=?", (book_id,))
    ctx.progress(job, len(chapters), len(chapters), f"角色 {len(aggregate['characters'])} 个")
