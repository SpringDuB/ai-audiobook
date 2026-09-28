from . import jobs, store

# 分析链（书稿 → 角色 → 逐句情感 → 选角）与合成链（合成 → 渲染 → 合本）
ANALYSIS_KINDS = {"chapter_split", "characters", "lines", "casting"}
AUDIO_KINDS = {"synthesize", "post", "book_export"}
PHASE_KINDS = {"analysis": ANALYSIS_KINDS, "audio": AUDIO_KINDS}


def plan_book(settings, conn, book_id: str, phase: str = "all") -> list[tuple[str, int | None]]:
    """按"文件即断点"决定下一步该入队哪些任务。"""
    plan = _plan_all(settings, book_id)
    allowed = PHASE_KINDS.get(phase)
    if allowed is None:
        return plan
    return [item for item in plan if item[0] in allowed]


def _plan_all(settings, book_id: str) -> list[tuple[str, int | None]]:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        return [("chapter_split", None)]
    if not store.characters_path(settings, book_id).exists():
        return [("characters", None)]

    plan: list[tuple[str, int | None]] = []
    for chapter in chapters:
        index = chapter["index"]
        if not store.lines_path(settings, book_id, index).exists():
            plan.append(("lines", index))
    if plan:
        return plan
    if not store.casting_path(settings, book_id).exists():
        return [("casting", None)]

    missing: list[tuple[str, int | None]] = []
    stale: list[tuple[str, int | None]] = []
    for chapter in chapters:
        index = chapter["index"]
        if not store.chapter_wav_path(settings, book_id, index).exists():
            missing.append(("synthesize", index))
        elif not store.chapter_render_meta_path(settings, book_id, index).exists():
            # 音频在但没按当前设置渲染过（例如 M2 时代产出的章节）→ 用 post 补渲染
            stale.append(("post", index))
    if missing:
        return missing
    if stale:
        return stale
    # 全部章节都有成品音频 → 收尾出整本
    if not store.book_wav_path(settings, book_id).exists():
        return [("book_export", None)]
    return []


def enqueue_plan(conn, book_id: str, plan) -> list[int]:
    return [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]


def resume_book(settings, conn, book_id: str, phase: str = "all") -> list[tuple[str, int | None]]:
    plan = plan_book(settings, conn, book_id, phase)
    enqueue_plan(conn, book_id, plan)
    return plan
