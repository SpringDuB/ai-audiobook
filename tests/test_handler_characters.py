from audiobook import jobs, store
from audiobook.handlers import characters as characters_handler  # noqa: F401  导入即注册
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

SAMPLE = "第一章 开始\n\n苏锐看着他。\n\n第二章 继续\n\n王胖子笑了。"

PASS_A_JSON = {
    "characters": [
        {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [],
}


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def test_characters_handler_writes_file_and_enqueues_scenes(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = _ctx(settings, conn, FakeLLM(routes={"PASS_A": PASS_A_JSON}))

    assert run_once(ctx) is True  # chapter_split
    assert run_once(ctx) is True  # characters

    payload = store.read_json(store.characters_path(settings, book_id))
    assert payload["book_id"] == book_id
    assert [c["name"] for c in payload["characters"]] == ["旁白", "苏锐"]
    assert payload["characters"][1]["id"] == "role_0001"
    scenes = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "scenes"]
    assert sorted(j.chapter_index for j in scenes) == [0, 1]
    assert not store.lines_path(settings, book_id, 1).exists()  # 行由 lines handler 产出
    assert all(row["pass"] == "A" for row in store.read_jsonl(store.llm_log_path(settings, book_id)))


def test_characters_handler_skips_failed_chapter_and_records_issue(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    llm = FakeLLM(routes={"PASS_A": PASS_A_JSON}, fail_on={"第二章"})
    ctx = _ctx(settings, conn, llm)

    run_once(ctx)  # split
    run_once(ctx)  # characters

    issues = store.read_jsonl(store.issues_path(settings, book_id))
    assert [issue["kind"] for issue in issues] == ["pass_a_chapter_skipped"]
    assert issues[0]["chapter"] == 1
    assert len(store.read_json(store.characters_path(settings, book_id))["characters"]) >= 2


def test_analysis_handlers_require_llm(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", llm=None)

    run_once(ctx)  # split
    run_once(ctx)  # characters → 失败

    job = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "characters"][0]
    assert job.status == "queued"  # 计入重试
    row = conn.execute("SELECT error FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert "未配置 LLM" in row["error"]
