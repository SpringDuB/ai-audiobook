from audiobook import jobs, store
from audiobook.handlers import characters as characters_handler  # noqa: F401
from audiobook.handlers import scenes as scenes_handler  # noqa: F401
from audiobook.handlers import split  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

SAMPLE = "第一章 开始\n\n第一句。第二句。"
PASS_A_JSON = {
    "characters": [
        {"name": "苏锐", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [],
}


def test_scenes_handler_writes_scene_file_and_enqueues_lines(settings, conn, tmp_path):
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")
    llm = FakeLLM(
        routes={
            "PASS_A": PASS_A_JSON,
            "PASS_B": {
                "scenes": [
                    {"index": 1, "title": "开场", "summary": "概括", "participants": ["苏锐"],
                     "tone": "平静", "tone_intensity": 0.4}
                ]
            },
        }
    )
    ctx = WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )
    run_once(ctx)  # split
    run_once(ctx)  # characters
    run_once(ctx)  # scenes

    payload = store.read_json(store.scenes_path(settings, book_id, 0))
    assert payload["scenes"][0]["id"] == "c0000-s01"
    assert payload["scenes"][0]["start_line"] == 1
    assert payload["scenes"][0]["participants"] == ["role_0001"]
    assert [j.kind for j in jobs.list_jobs(conn, book_id) if j.kind == "lines"] == ["lines"]
