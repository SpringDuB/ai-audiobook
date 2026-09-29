"""多章批量分析 handler：一个 job 内部并行提取勾选章节，其余章不动。"""

import threading
import time

from audiobook import jobs, store
from audiobook.handlers import chapter_batch  # noqa: F401  导入即注册
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

SAMPLE = (
    "第一章 开始\n\n苏锐站在门口。\n\n"
    "第二章 继续\n\n小鹿：走。\n\n"
    "第三章 收尾\n\n小鹿：等我。\n"
)
MERGED = {"characters": [{"name": "小鹿", "aliases": []}]}


def _extract_route(user: str) -> list[dict]:
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
            rows.append({"text": line, "role": "旁白"})
    return rows


class SlowFakeLLM(FakeLLM):
    """记录同时在跑的请求数：串行实现峰值只会是 1。"""

    def __init__(self, *args, delay: float = 0.2, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self._lock = threading.Lock()
        self.inflight = 0
        self.peak = 0

    def complete(self, system: str, user: str, *, max_output_tokens: int = 4096):
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.delay)
            return super().complete(system, user, max_output_tokens=max_output_tokens)
        finally:
            with self._lock:
                self.inflight -= 1


def _llm(**kwargs) -> FakeLLM:
    routes = {"【EXTRACT】": _extract_route, "【MERGE_ROLES】": MERGED}
    routes.update(kwargs.pop("routes", {}))
    return FakeLLM(routes=routes, **kwargs)


def _ctx(settings, conn, llm, concurrency: int = 4) -> WorkerContext:
    return WorkerContext(
        settings=settings.model_copy(update={"llm_concurrency": concurrency}),
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=concurrency), settings, max_attempts=2),
    )


def _seed(settings, conn, tmp_path) -> str:
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    run_once(_ctx(settings, conn, _llm()))  # chapter_split
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {
            "book_id": book_id,
            "characters": [
                {
                    "id": "narrator",
                    "name": "旁白",
                    "aliases": [],
                    "chapters": [0],
                    "mentions": 1,
                    "first": [0, 0],
                    "is_narrator": True,
                },
                {
                    "id": "role_0001",
                    "name": "苏锐",
                    "aliases": [],
                    "chapters": [0],
                    "mentions": 1,
                    "first": [0, 0],
                    "is_narrator": False,
                },
            ],
            "names": {"旁白": "narrator", "苏锐": "role_0001"},
        },
    )
    store.atomic_replace_json(store.extract_path(settings, book_id, 0), {"windows": 1, "lines": []})
    return book_id


def test_batch_handler_extracts_picked_chapters_and_keeps_others(settings, conn, tmp_path):
    book_id = _seed(settings, conn, tmp_path)
    untouched = store.extract_path(settings, book_id, 0).read_text(encoding="utf-8")
    ctx = _ctx(settings, conn, _llm())
    jobs.enqueue(conn, "chapters", book_id, payload={"chapters": [1, 2]})

    assert run_once(ctx) is True

    # 勾选的章重新提取并落行（不等整批跑完，边提取边落盘）
    first = store.read_json(store.extract_path(settings, book_id, 1))["lines"][0]
    assert first["text"] == "走。" and first["role"] == "小鹿"
    assert store.read_jsonl(store.lines_path(settings, book_id, 1))[0]["speaker_name"] == "小鹿"
    assert store.read_jsonl(store.lines_path(settings, book_id, 2))
    # 没勾的章没被碰过
    assert store.extract_path(settings, book_id, 0).read_text(encoding="utf-8") == untouched
    assert not store.lines_path(settings, book_id, 0).exists()

    # 角色表：老角色 id 保留，新称呼并进来，章号/台词数按真实章节记
    payload = store.read_json(store.characters_path(settings, book_id))
    by_name = {character["name"]: character for character in payload["characters"]}
    assert set(by_name) == {"旁白", "苏锐", "小鹿"}
    assert by_name["苏锐"]["id"] == "role_0001"
    assert by_name["小鹿"]["chapters"] == [1, 2]
    assert by_name["小鹿"]["mentions"] == 2

    # 收尾：只给勾选章入队 lines（用最终角色表再物化一次）
    line_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "lines"]
    assert sorted(j.chapter_index for j in line_jobs) == [1, 2]
    passes = [row["pass"] for row in store.read_jsonl(store.llm_log_path(settings, book_id))]
    assert sorted(passes) == ["extract", "extract", "merge"]


def test_batch_handler_runs_extractions_concurrently(settings, conn, tmp_path):
    book_id = _seed(settings, conn, tmp_path)
    llm = SlowFakeLLM(routes={"【EXTRACT】": _extract_route, "【MERGE_ROLES】": MERGED})
    ctx = _ctx(settings, conn, llm, concurrency=4)
    jobs.enqueue(conn, "chapters", book_id, payload={"chapters": [0, 1, 2]})

    assert run_once(ctx) is True

    # 串行实现（一次只发一个请求）峰值只能是 1
    assert llm.peak >= 2


def test_batch_handler_keeps_going_when_one_chapter_falls_back(settings, conn, tmp_path):
    book_id = _seed(settings, conn, tmp_path)
    ctx = _ctx(settings, conn, _llm(fail_on={"第三章"}))
    jobs.enqueue(conn, "chapters", book_id, payload={"chapters": [1, 2]})

    assert run_once(ctx) is True

    issues = store.read_jsonl(store.issues_path(settings, book_id))
    failed = [issue for issue in issues if issue["kind"] == "extract_window_failed"]
    assert [issue["chapter"] for issue in failed] == [2]
    # 失败的那一段原文不丢：整段按旁白落盘，其它勾选章照常
    fallback = store.read_json(store.extract_path(settings, book_id, 2))["lines"]
    assert [line["role"] for line in fallback] == ["旁白"]
    assert store.read_jsonl(store.lines_path(settings, book_id, 1))
    payload = store.read_json(store.characters_path(settings, book_id))
    assert "小鹿" in {character["name"] for character in payload["characters"]}
