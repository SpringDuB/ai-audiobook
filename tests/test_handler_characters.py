"""角色分析 handler：整章一趟出「角色 + 关系 + 每句标注」，逐句落盘交给 lines。"""

from audiobook import jobs, store
from audiobook.handlers import characters as characters_handler  # noqa: F401  导入即注册
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.pipeline import resume_book
from audiobook.worker import WorkerContext, run_once

SAMPLE = "第一章 开始\n\n苏锐看着他。\n\n第二章 继续\n\n王胖子笑了。"

CARDS = [
    {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年"},
    {"name": "旁白", "gender": "未知", "age_group": "未知"},
]


def _route(user: str) -> dict:
    tail = user.split("需要标注的句子：", 1)[-1]
    rows = []
    for line in tail.splitlines():
        if ". " not in line:
            continue
        number, body = line.split(". ", 1)
        if not number.strip().isdigit():
            continue
        speaker = "苏锐" if "苏锐" in body else "旁白"
        rows.append({"index": int(number), "speaker": speaker, "emotion": "平静", "intensity": 0.4})
    return {"characters": CARDS, "relationships": [], "lines": rows}


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def test_characters_handler_writes_file_and_enqueues_lines(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = _ctx(settings, conn, FakeLLM(routes={"CHAPTER_ANALYSIS": _route}))

    assert run_once(ctx) is True  # chapter_split
    assert run_once(ctx) is False  # 导入不会自己跑 LLM
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「分析角色文本」
    assert run_once(ctx) is True  # characters：整章分析

    payload = store.read_json(store.characters_path(settings, book_id))
    assert payload["book_id"] == book_id
    assert [c["name"] for c in payload["characters"]] == ["旁白", "苏锐"]
    assert payload["characters"][1]["id"] == "role_0001"
    # 每章都留下了原始整章分析（角色 + 每句标注）
    for index in (0, 1):
        raw = store.read_json(store.chapter_analysis_path(settings, book_id, index))
        assert raw["characters"] and raw["lines"]
    line_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "lines"]
    assert sorted(j.chapter_index for j in line_jobs) == [0, 1]
    assert not store.lines_path(settings, book_id, 1).exists()  # 行由 lines handler 产出
    assert all(row["pass"] == "A" for row in store.read_jsonl(store.llm_log_path(settings, book_id)))


def test_characters_handler_records_window_failure_and_keeps_other_chapters(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route}, fail_on={"第二章"})
    ctx = _ctx(settings, conn, llm)

    run_once(ctx)  # split
    resume_book(settings, conn, book_id, phase="analysis")
    run_once(ctx)  # characters

    issues = store.read_jsonl(store.issues_path(settings, book_id))
    kinds = [issue["kind"] for issue in issues]
    assert "chapter_window_failed" in kinds and "line_index_missing" in kinds
    failed = next(issue for issue in issues if issue["kind"] == "chapter_window_failed")
    assert failed["chapter"] == 1
    assert len(store.read_json(store.characters_path(settings, book_id))["characters"]) >= 2
    assert store.read_json(store.chapter_analysis_path(settings, book_id, 1))["lines"] == []
    # 第一章不受影响
    assert store.read_json(store.chapter_analysis_path(settings, book_id, 0))["lines"]


def test_analysis_handlers_require_llm(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", llm=None)

    run_once(ctx)  # split
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「一键分析」
    run_once(ctx)  # characters → 失败

    job = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "characters"][0]
    assert job.status == "queued"  # 计入重试
    row = conn.execute("SELECT error FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert "未配置 LLM" in row["error"]
