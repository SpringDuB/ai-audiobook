"""角色分析 handler：逐章 LLM 提取 → 全书 LLM 整合 → 每章入队 lines。"""

from audiobook import jobs, store
from audiobook.handlers import characters as characters_handler  # noqa: F401  导入即注册
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.pipeline import resume_book
from audiobook.worker import WorkerContext, run_once

SAMPLE = "第一章 开始\n\n苏锐站在门口。\n\n第二章 继续\n\n小鹿：走。\n"
MERGED = {"characters": [{"name": "小鹿", "aliases": []}]}


def _extract_route(user: str) -> list[dict]:
    """假模型：带 "X：" 前缀的按规则剥前缀，其余按旁白。"""
    body = user.split("【正文】", 1)[-1]
    rows: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "：" in line and not line.startswith("“"):
            name, rest = line.split("：", 1)
            rows.append({"text": rest, "role": name, "emotion": "平静", "intensity": 0.4})
        else:
            rows.append({"text": line, "role": "旁白", "emotion": None})
    return rows


def _llm(**kwargs) -> FakeLLM:
    routes = {"【EXTRACT】": _extract_route, "【MERGE_ROLES】": MERGED}
    routes.update(kwargs.pop("routes", {}))
    return FakeLLM(routes=routes, **kwargs)


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def test_characters_handler_extracts_then_merges_and_enqueues_lines(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = _ctx(settings, conn, _llm())

    assert run_once(ctx) is True  # chapter_split
    assert run_once(ctx) is False  # 导入不会自己跑 LLM
    resume_book(settings, conn, book_id, phase="analysis")  # 用户点「分析角色文本」
    assert run_once(ctx) is True  # characters：提取 + 整合

    payload = store.read_json(store.characters_path(settings, book_id))
    assert payload["book_id"] == book_id
    assert [c["name"] for c in payload["characters"]] == ["旁白", "小鹿"]
    assert payload["characters"][1]["id"] == "role_0001"
    # 每章都留下了提取结果（句子 + 说话人 + 情绪）
    for index in (0, 1):
        raw = store.read_json(store.extract_path(settings, book_id, index))
        assert raw["lines"]
    first = store.read_json(store.extract_path(settings, book_id, 1))["lines"][0]
    assert first["text"] == "走。" and first["role"] == "小鹿"
    assert first["emotion"] == "平静" and first["intensity"] == 0.4
    line_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "lines"]
    assert sorted(j.chapter_index for j in line_jobs) == [0, 1]
    assert not store.lines_path(settings, book_id, 1).exists()  # 行由 lines handler 产出
    passes = [row["pass"] for row in store.read_jsonl(store.llm_log_path(settings, book_id))]
    assert sorted(passes) == ["extract", "extract", "merge"]


def test_characters_handler_keeps_other_chapters_when_one_chapter_fails(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = _ctx(settings, conn, _llm(fail_on={"第二章 继续"}))

    run_once(ctx)  # split
    resume_book(settings, conn, book_id, phase="analysis")
    run_once(ctx)  # characters

    issues = store.read_jsonl(store.issues_path(settings, book_id))
    failed = [issue for issue in issues if issue["kind"] == "extract_window_failed"]
    assert [issue["chapter"] for issue in failed] == [1]
    # 失败的那一段原文不丢：整段按旁白落盘（说话人/情绪丢失，异常清单可见）
    fallback = store.read_json(store.extract_path(settings, book_id, 1))["lines"]
    assert [line["role"] for line in fallback] == ["旁白"]
    assert store.read_json(store.extract_path(settings, book_id, 0))["lines"]
    # 失败章不参与整合，但已经提取出来的角色照常落盘
    payload = store.read_json(store.characters_path(settings, book_id))
    assert [c["name"] for c in payload["characters"]] == ["旁白"]


def test_analysis_handlers_require_llm(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", llm=None)

    run_once(ctx)  # split
    resume_book(settings, conn, book_id, phase="analysis")
    run_once(ctx)  # characters → 失败

    job = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "characters"][0]
    assert job.status == "queued"  # 计入重试
    row = conn.execute("SELECT error FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert "未配置 LLM" in row["error"]
