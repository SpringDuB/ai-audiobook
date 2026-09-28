from audiobook import audio, jobs, store
from audiobook.api.app import create_app  # noqa: F401  确保导入链路完整
from audiobook.db import connect, init_db
from fake_engine import FakeEngine
from audiobook.handlers import casting, characters, lines, post, scenes, split, synthesize  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.pipeline import resume_book
from audiobook.worker import WorkerContext, run_once

SAMPLE = (
    "简介：一个修仙者的故事。\n\n"
    "第一章 重生十年前\n\n"
    "苏锐站在院子里。\n\n"
    "“老苏，你怎么看？”王胖子问道。\n\n"
    "第二章 死党王胖子\n\n"
    "苏锐说：“胖子，别废话。”\n"
)


def _route_a(user: str) -> dict:
    """按章返回不同角色：王胖子只出现在含"胖子"的章，让角色 id 排序有确定性。"""
    people = [
        {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年", "personality": ["冷静"]},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ]
    relationships = []
    if "胖子" in user:
        people.insert(1, {"name": "王胖子", "aliases": ["胖子"], "gender": "男", "age_group": "青年"})
        relationships.append(
            {"from": "王胖子", "to": "苏锐", "closeness": 0.8, "hierarchy": 0.2, "hostility": 0.0, "intimacy": 0.6}
        )
    return {"characters": people, "relationships": relationships}


PASS_B = {
    "scenes": [
        {"index": 1, "title": "开场", "summary": "两人对话", "participants": ["苏锐", "王胖子"],
         "tone": "平静", "tone_intensity": 0.4}
    ]
}


def _sentences_from_prompt(user: str) -> list[str]:
    tail = user.split("句子列表：", 1)[-1]
    return [line.split(". ", 1)[1] for line in tail.splitlines() if ". " in line and line[:1].isdigit()]


def _route_c(user: str) -> dict:
    lines = []
    for position, sentence in enumerate(_sentences_from_prompt(user), start=1):
        quoted = "“" in sentence
        if "老苏" in sentence:
            speaker, addressee = "王胖子", "老苏"
        elif quoted and "苏锐" in sentence:
            speaker, addressee = "苏锐", None
        elif quoted and "王胖子" in sentence:
            speaker, addressee = "王胖子", None
        else:
            speaker, addressee = "旁白", None
        lines.append(
            {
                "index": position,
                "speaker": speaker,
                "addressee": addressee,
                "emotion": "平静",
                "intensity": 0.4,
            }
        )
    return {"lines": lines}


def _ctx(settings, conn, engine=None) -> WorkerContext:
    llm = FakeLLM(routes={"PASS_A": _route_a, "PASS_B": PASS_B, "PASS_C": _route_c})
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        engine=engine or FakeEngine(ms_per_char=10.0),
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def _drain(ctx) -> None:
    """跑到队列空；任务链不收敛就直接失败，免得测试挂死。"""
    guard = 0
    while run_once(ctx):
        guard += 1
        assert guard < 100, "任务链没有收敛"


def test_full_analysis_pipeline_without_network_produces_chapter_artifacts(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    txt = tmp_path / "地球最后一个修仙者.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="地球最后一个修仙者")
    ctx = _ctx(settings, conn)

    # 导入只自动分章：LLM 要等用户点「一键分析」
    _drain(ctx)
    assert store.read_json(store.characters_path(settings, book_id)) is None

    resume_book(settings, conn, book_id, phase="analysis")
    _drain(ctx)

    analysis = store.read_json(store.characters_path(settings, book_id))
    assert analysis["characters"][0]["id"] == "narrator"
    assert [c["name"] for c in analysis["characters"][1:]] == ["苏锐", "王胖子"]

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    assert [row["speaker"] for row in rows] == ["narrator", "role_0002", "narrator"]
    assert rows[1]["addressee"] == "role_0001"  # “老苏”被解析成别名
    assert rows[1]["emotion"]["source"] == "line"
    rows_ch2 = store.read_jsonl(store.lines_path(settings, book_id, 2))
    assert [row["speaker"] for row in rows_ch2] == ["role_0001"]

    casting = store.read_json(store.casting_path(settings, book_id))
    assert casting["voice_library_size"] == 0
    assert casting["names"]["老苏"] == "role_0001"

    # 分析跑完不该自己开始合成：那是「生成有声书」的事
    assert [j for j in jobs.list_jobs(conn, book_id) if j.status == "queued"] == []
    assert not store.output_dir(settings, book_id).exists()

    resume_book(settings, conn, book_id, phase="audio")
    _drain(ctx)

    out = store.output_dir(settings, book_id)
    assert (out / "chapter_0001.wav").exists() and (out / "chapter_0001.srt").exists()
    assert (out / "chapter_0002.wav").exists()
    assert audio.wav_duration(out / "chapter_0001.wav") > 0.1
    assert all(j.status == "done" for j in jobs.list_jobs(conn, book_id))

    log = store.read_jsonl(store.llm_log_path(settings, book_id))
    assert {row["pass"] for row in log} == {"A", "B", "C"}
    assert all(row["ok"] is True for row in log)


def test_rerun_after_line_change_only_regenerates_changed_line(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    txt = tmp_path / "b.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="T")

    engine = FakeEngine(ms_per_char=10.0)
    calls = {"n": 0}
    original = engine.synthesize

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    engine.synthesize = counting  # type: ignore[method-assign]
    ctx = _ctx(settings, conn, engine=engine)
    _drain(ctx)                                            # 分章
    resume_book(settings, conn, book_id, phase="analysis")  # 「一键分析」
    _drain(ctx)
    resume_book(settings, conn, book_id, phase="audio")     # 「一键生成」
    _drain(ctx)
    first = calls["n"]

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    rows[1]["text"] = "改过的第二句。"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 1), rows)
    jobs.enqueue(conn, "synthesize", book_id, 1)
    while run_once(ctx):
        pass

    assert calls["n"] == first + 1
