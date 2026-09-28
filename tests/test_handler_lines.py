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
PASS_A_JSON = {
    "characters": [
        {"name": "苏锐", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "王胖子", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [],
}


def _sentences_from_prompt(user: str) -> list[str]:
    tail = user.split("句子列表：", 1)[-1]
    return [line.split(". ", 1)[1] for line in tail.splitlines() if ". " in line and line[:1].isdigit()]


def _route_c(user: str) -> dict:
    lines = []
    for position, sentence in enumerate(_sentences_from_prompt(user), start=1):
        if "苏锐" in sentence:
            speaker = "苏锐"
        elif "王胖子" in sentence:
            speaker = "王胖子"
        else:
            speaker = "旁白"
        lines.append({"index": position, "speaker": speaker, "emotion": "平静", "intensity": 0.4})
    return {"lines": lines}


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
    llm = FakeLLM(routes={"PASS_A": PASS_A_JSON, "PASS_C": _route_c})
    ctx = _ctx(settings, conn, llm)
    run_once(ctx)  # 导入 → 分章
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「一键分析」
    for _ in range(3):
        run_once(ctx)  # characters → lines → casting

    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    assert [row["speaker"] for row in rows] == ["role_0001", "role_0002"]
    assert [row["id"] for row in rows] == ["c0000-s01-l001", "c0000-s01-l002"]
    assert rows[0]["emotion"]["source"] == "line"
    casting_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "casting"]
    assert len(casting_jobs) == 1
    assert casting_ready(settings, conn, book_id) is True


def test_casting_not_ready_while_analysis_jobs_are_active(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    jobs.enqueue(conn, "lines", book_id, 1)
    assert casting_ready(settings, conn, book_id) is False


def test_lines_handler_degrades_window_failure_to_narrator(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    llm = FakeLLM(routes={"PASS_A": PASS_A_JSON, "PASS_C": {"lines": []}}, fail_on={"PASS_C"})
    ctx = _ctx(settings, conn, llm)
    run_once(ctx)  # 导入 → 分章
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「一键分析」
    for _ in range(2):
        run_once(ctx)  # characters → lines

    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    assert rows and all(row["speaker"] == "narrator" for row in rows)
    kinds = [issue["kind"] for issue in store.read_jsonl(store.issues_path(settings, book_id))]
    assert kinds == ["pass_c_failed"]
