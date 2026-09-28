"""端到端：导入 → 分析角色文本（提取/整合/推荐）→ 生成有声书（合成/渲染/合本）。

全程离线：LLM 用 FakeLLM（按提示词标记路由），TTS 用 FakeEngine。
这与产品里"只有真引擎"的约束不冲突——替身只存在于 tests/。
"""

from audiobook import audio, jobs, store
from audiobook.api.app import create_app  # noqa: F401  确保导入链路完整
from audiobook.db import connect, init_db
from fake_engine import FakeEngine
from audiobook.handlers import book_export, casting, characters, lines, post, split, synthesize  # noqa: F401
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


def _route_extract(user: str) -> list[dict]:
    """假模型：引语归说话人、归属句留在旁白、X： 前缀剥掉。"""
    body = user.split("【正文】", 1)[-1]
    rows: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("“") and "”" in line:
            end = line.index("”") + 1
            rows.append({"text": line[:end], "role": "王胖子", "emotion": "喜悦", "intensity": 0.6})
            tail = line[end:]
            if tail:
                rows.append({"text": tail, "role": "旁白", "emotion": None})
        elif "说：“" in line:
            head, rest = line.split("说：“", 1)
            rows.append({"text": f"{head}说：", "role": "旁白", "emotion": None})
            rows.append({"text": f"“{rest}", "role": head, "emotion": "愤怒", "intensity": 0.8})
        else:
            rows.append({"text": line, "role": "旁白", "emotion": None})
    return rows


def _route_merge(user: str) -> dict:
    people = []
    if "苏锐" in user:
        people.append({"name": "苏锐", "aliases": []})
    if "王胖子" in user:
        people.append({"name": "王胖子", "aliases": []})
    return {"characters": people}


def _route_recommend(user: str) -> dict:
    if "角色：旁白" in user:
        return {"recommendations": [{"voiceId": "v_nar", "confidence": 0.9, "reason": "叙述平稳"}]}
    return {"recommendations": [{"voiceId": "v_hero", "confidence": 0.85, "reason": "冷峻克制"}]}


def _seed_voices(settings) -> None:
    for voice_id, name, usage in (("v_nar", "沉稳旁白", "旁白叙述"), ("v_hero", "冷峻男声", "角色对话")):
        store.atomic_replace_json(
            settings.voices_dir / voice_id / "voice.json",
            {
                "id": voice_id,
                "name": name,
                "gender": "男",
                "age_group": "青年",
                "speech_rate": "中",
                "personality": ["冷静"],
                "genres": ["都市"],
                "mood": ["沉稳"],
                "voice_quality": ["磁性"],
                "usage_type": [usage],
                "description": "测试用音色",
            },
        )


def _ctx(settings, conn, engine=None) -> WorkerContext:
    llm = FakeLLM(
        routes={
            "【EXTRACT】": _route_extract,
            "【MERGE_ROLES】": _route_merge,
            "【VOICE_RECOMMEND】": _route_recommend,
        }
    )
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


def test_full_pipeline_offline_produces_chapter_and_book_artifacts(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    _seed_voices(settings)
    txt = tmp_path / "地球最后一个修仙者.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="地球最后一个修仙者")
    ctx = _ctx(settings, conn)

    # 导入只自动分章：LLM 要等用户点「分析角色文本」
    _drain(ctx)
    assert store.read_json(store.characters_path(settings, book_id)) is None

    resume_book(settings, conn, book_id, phase="analysis")
    _drain(ctx)

    analysis = store.read_json(store.characters_path(settings, book_id))
    assert analysis["characters"][0]["id"] == "narrator"
    assert {c["name"] for c in analysis["characters"]} == {"旁白", "苏锐", "王胖子"}
    assert analysis["names"]["王胖子"] == "role_0002"
    assert analysis["names"]["苏锐"] == "role_0001"

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    assert [row["kind"] for row in rows] == ["narration", "dialogue", "narration"]
    assert [row["speaker"] for row in rows] == ["narrator", "role_0002", "narrator"]
    assert rows[1]["emotion"]["source"] == "line"
    assert rows[1]["emotion"]["mix"] == [{"name": "喜悦", "weight": 0.6}]
    assert rows[0]["emotion"] == {"dominant": "平静", "intensity": 0.0, "source": "none"}
    rows_ch2 = store.read_jsonl(store.lines_path(settings, book_id, 2))
    assert [row["kind"] for row in rows_ch2] == ["narration", "dialogue"]
    assert [row["speaker"] for row in rows_ch2] == ["narrator", "role_0001"]

    casting = store.read_json(store.casting_path(settings, book_id))
    assert casting["narrator_voice"] == "v_nar"
    assert casting["roles"]["role_0001"]["voice_id"] == "v_hero"
    assert casting["roles"]["role_0001"]["recommendations"][0]["voice_name"] == "冷峻男声"
    assert casting["names"]["苏锐"] == "role_0001"

    # 分析跑完不该自己开始合成：那是「生成有声书」的事
    assert [j for j in jobs.list_jobs(conn, book_id) if j.status == "queued"] == []
    assert not store.output_dir(settings, book_id).exists()

    resume_book(settings, conn, book_id, phase="audio")
    _drain(ctx)

    out = store.output_dir(settings, book_id)
    assert (out / "chapter_0001.wav").exists() and (out / "chapter_0001.srt").exists()
    assert (out / "chapter_0002.wav").exists()
    assert audio.wav_duration(out / "chapter_0001.wav") > 0.1

    # 全部章节都有成品后，再点一次「生成有声书 / 导出成品」就合出整本
    resume_book(settings, conn, book_id, phase="audio")
    _drain(ctx)
    assert (out / "book.wav").exists() and (out / "book.srt").exists()
    assert all(j.status == "done" for j in jobs.list_jobs(conn, book_id))

    passes = {row["pass"] for row in store.read_jsonl(store.llm_log_path(settings, book_id))}
    assert passes == {"extract", "merge", "casting"}


def test_rerun_after_line_change_only_regenerates_changed_line(settings, tmp_path):
    conn = connect(settings.db_path)
    init_db(conn)
    _seed_voices(settings)
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
    _drain(ctx)                                             # 分章
    resume_book(settings, conn, book_id, phase="analysis")   # 「分析角色文本」
    _drain(ctx)
    resume_book(settings, conn, book_id, phase="audio")      # 「生成有声书」
    _drain(ctx)
    first = calls["n"]

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    rows[1]["text"] = "改过的第二句。"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 1), rows)
    jobs.enqueue(conn, "synthesize", book_id, 1)
    while run_once(ctx):
        pass

    assert calls["n"] == first + 1
