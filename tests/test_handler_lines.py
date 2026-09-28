"""lines handler：角色分析已经出过逐句标注，这里只做「名字 → role_id」的落盘。"""

from audiobook import jobs, store
from audiobook.analysis.readiness import casting_ready
from audiobook.handlers import characters as characters_handler  # noqa: F401
from audiobook.handlers import lines as lines_handler  # noqa: F401
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.pipeline import resume_book
from audiobook.worker import WorkerContext, run_once

SAMPLE = "第一章 开始\n\n苏锐说：“走。”\n\n王胖子说：“好。”"

CARDS = [
    {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年"},
    {"name": "王胖子", "aliases": [], "gender": "男", "age_group": "青年"},
    {"name": "旁白", "gender": "未知", "age_group": "未知"},
]


def _route(user: str) -> dict:
    """照句子里的名字给说话人；两处引语自身没名字，靠归属句提示兜底。"""
    tail = user.split("需要标注的句子：", 1)[-1]
    rows = []
    for line in tail.splitlines():
        if ". " not in line:
            continue
        number, body = line.split(". ", 1)
        if not number.strip().isdigit():
            continue
        if "苏锐" in body:
            speaker = "苏锐"
        elif "王胖子" in body:
            speaker = "王胖子"
        else:
            speaker = "旁白"
        rows.append({"index": int(number), "speaker": speaker, "emotion": "平静", "intensity": 0.4})
    return {"characters": CARDS, "relationships": [], "lines": rows}


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def test_lines_handler_writes_lines_and_triggers_casting_when_ready(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    llm = FakeLLM(routes={"CHAPTER_ANALYSIS": _route})
    ctx = _ctx(settings, conn, llm)
    run_once(ctx)  # 导入 → 分章
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「分析角色文本」
    for _ in range(3):
        run_once(ctx)  # characters（整章分析）→ lines（落盘）→ casting

    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    # 归属句单独成旁白行，引语靠归属句提示落到角色身上
    assert [row["kind"] for row in rows] == ["narration", "dialogue", "narration", "dialogue"]
    assert [row["speaker"] for row in rows] == ["role_0001", "role_0001", "role_0002", "role_0002"]
    assert [row["id"] for row in rows] == [f"c0000-s01-l00{index}" for index in range(1, 5)]
    assert rows[0]["emotion"]["source"] == "line"
    casting_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "casting"]
    assert len(casting_jobs) == 1
    assert casting_ready(settings, conn, book_id) is True


def test_lines_handler_reruns_llm_only_when_chapter_analysis_is_missing(settings, conn, tmp_path):
    """「分析本章」会删掉本章原始分析 → lines 自己补跑一次 LLM，而不是摆烂。"""
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = _ctx(settings, conn, FakeLLM(routes={"CHAPTER_ANALYSIS": _route}))
    run_once(ctx)
    resume_book(settings, conn, book_id, phase="analysis")
    run_once(ctx)  # characters

    store.chapter_analysis_path(settings, book_id, 0).unlink()  # 模拟「分析本章」
    store.lines_path(settings, book_id, 0).unlink(missing_ok=True)
    jobs.enqueue(conn, "lines", book_id, 0)
    run_once(ctx)

    assert store.read_json(store.chapter_analysis_path(settings, book_id, 0))["lines"]
    assert store.read_jsonl(store.lines_path(settings, book_id, 0))


def test_casting_not_ready_while_analysis_jobs_are_active(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    jobs.enqueue(conn, "lines", book_id, 1)
    assert casting_ready(settings, conn, book_id) is False
