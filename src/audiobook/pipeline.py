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
        audio_path = store.output_dir(settings, book_id) / f"chapter_{store.chapter_tag(index)}.wav"
        if not audio_path.exists():
            missing.append(("synthesize", index))
    return missing


def enqueue_plan(conn, book_id: str, plan) -> list[int]:
    return [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]


def resume_book(settings, conn, book_id: str) -> list[tuple[str, int | None]]:
    plan = plan_book(settings, conn, book_id)
    enqueue_plan(conn, book_id, plan)
    return plan
