from . import jobs, store
from .analysis.casting import voice_for_speaker
from .render.chapter import RENDER_VERSION

# 分析链（书稿 → 角色 → 逐句情感 → 选角）与合成链（合成 → 渲染 → 合本）
ANALYSIS_KINDS = {"chapter_split", "characters", "lines", "casting"}
AUDIO_KINDS = {"synthesize", "post", "book_export"}
PHASE_KINDS = {"analysis": ANALYSIS_KINDS, "audio": AUDIO_KINDS}


def plan_book(settings, conn, book_id: str, phase: str = "all", force: bool = False) -> list[tuple[str, int | None]]:
    """按"文件即断点"决定下一步该入队哪些任务。force=True 时不看断点，整本重跑分析链。"""
    plan = _plan_all(settings, book_id, force=force)
    allowed = PHASE_KINDS.get(phase)
    if allowed is None:
        return plan
    return [item for item in plan if item[0] in allowed]


def _plan_all(settings, book_id: str, force: bool = False) -> list[tuple[str, int | None]]:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    if not chapters:
        return [("chapter_split", None)]
    if force:
        # 整章分析一趟出角色 + 每句情感：character 任务负责全书，lines 任务只做落盘
        return [("characters", None), *[("lines", chapter["index"]) for chapter in chapters]]
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

    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    missing: list[tuple[str, int | None]] = []
    stale: list[tuple[str, int | None]] = []
    for chapter in chapters:
        index = chapter["index"]
        if not store.chapter_wav_path(settings, book_id, index).exists():
            missing.append(("synthesize", index))
        elif _voice_stale(settings, book_id, index, casting):
            # 换过音色：这一章要按新音色重合成（缓存键会只让受影响的行重跑）
            missing.append(("synthesize", index))
        elif not _render_current(settings, book_id, index):
            # 音频在但没有按当前渲染版本出过成品（例如删停顿前的旧产物）→ 用 post 补渲染
            stale.append(("post", index))
    if missing:
        return missing
    if stale:
        return stale
    # 全部章节都有成品音频 → 收尾出整本
    if not store.book_wav_path(settings, book_id).exists():
        return [("book_export", None)]
    return []


def _voice_stale(settings, book_id: str, index: int, casting: dict) -> bool:
    """逐句音频记的音色和当前选角不一致吗？（换音色后要真的重合成，不能拿旧片段重渲染）"""
    if not casting.get("roles"):
        return False
    rows = store.read_jsonl(store.lines_path(settings, book_id, index))
    clips = store.audio_dir(settings, book_id, index)
    for row in rows:
        speaker = row.get("speaker")
        if not speaker:
            continue
        expected = voice_for_speaker(casting, speaker)
        if not expected:
            continue
        meta = store.read_json(clips / f"{row['id']}.meta.json", default={}) or {}
        recorded = meta.get("voice_id")
        if recorded and recorded != expected:
            return True
    return False


def _render_current(settings, book_id: str, index: int) -> bool:
    """章节成品是不是当前渲染版本？旧版本（带句间停顿）要重新渲染。"""
    meta = store.read_json(store.chapter_render_meta_path(settings, book_id, index), default={}) or {}
    return int(meta.get("render_version") or 0) >= RENDER_VERSION


def enqueue_plan(conn, book_id: str, plan) -> list[int]:
    return [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]


def resume_book(settings, conn, book_id: str, phase: str = "all", force: bool = False) -> list[tuple[str, int | None]]:
    plan = plan_book(settings, conn, book_id, phase, force=force)
    enqueue_plan(conn, book_id, plan)
    return plan
