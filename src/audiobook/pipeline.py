from . import jobs, store


def plan_book(settings, conn, book_id: str) -> list[tuple[str, int | None]]:
    """按"文件即断点"决定下一步该入队哪些任务。"""
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        return [("chapter_split", None)]
    if not store.characters_path(settings, book_id).exists():
        return [("characters", None)]

    plan: list[tuple[str, int | None]] = []
    for chapter in chapters:
        index = chapter["index"]
        if not store.scenes_path(settings, book_id, index).exists():
            plan.append(("scenes", index))
        elif not store.lines_path(settings, book_id, index).exists():
            plan.append(("lines", index))
    if plan:
        return plan
    if not store.casting_path(settings, book_id).exists():
        return [("casting", None)]

    missing: list[tuple[str, int | None]] = []
    for chapter in chapters:
        index = chapter["index"]
        if not store.chapter_wav_path(settings, book_id, index).exists():
            missing.append(("synthesize", index))
    if missing:
        return missing
    # 全部章节都有成品音频 → 收尾出整本
    if not store.book_wav_path(settings, book_id).exists():
        return [("book_export", None)]
    return []


def enqueue_plan(conn, book_id: str, plan) -> list[int]:
    return [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]


def resume_book(settings, conn, book_id: str) -> list[tuple[str, int | None]]:
    plan = plan_book(settings, conn, book_id)
    enqueue_plan(conn, book_id, plan)
    return plan
