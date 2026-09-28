"""casting handler：大模型推荐音色 → voices/casting.json（含推荐列表，默认用第一个）。"""

from audiobook import jobs, store
from audiobook.handlers import casting as casting_handler  # noqa: F401
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

CHARACTERS = {
    "book_id": "book1",
    "characters": [
        {"id": "narrator", "name": "旁白", "aliases": [], "chapters": [1], "mentions": 1, "is_narrator": True},
        {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "chapters": [1], "mentions": 1, "is_narrator": False},
    ],
    "names": {"旁白": "narrator", "苏锐": "role_0001", "老苏": "role_0001"},
}


def _seed_voice(settings, voice_id: str, name: str, **overrides) -> None:
    meta = {
        "id": voice_id,
        "name": name,
        "gender": "男",
        "age_group": "青年",
        "speech_rate": "中",
        "personality": ["冷静"],
        "genres": ["都市"],
        "mood": ["沉稳"],
        "voice_quality": ["磁性"],
        "usage_type": ["角色对话"],
        "description": "测试用音色",
    }
    meta.update(overrides)
    store.atomic_replace_json(settings.voices_dir / voice_id / "voice.json", meta)


def _seed_book(settings, book_id="book1") -> None:
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": 1, "title": "第一章", "content": "夜色很深。", "chars": 5}]},
    )
    store.write_jsonl_atomic(
        store.lines_path(settings, book_id, 1),
        [
            {"id": "c0001-s01-l001", "kind": "narration", "speaker": "narrator", "speaker_name": "旁白", "text": "夜色很深。"},
            {"id": "c0001-s01-l002", "kind": "dialogue", "speaker": "role_0001", "speaker_name": "苏锐", "text": "别废话。"},
        ],
    )


def _route(user: str) -> dict:
    if "角色：旁白" in user:
        return {"recommendations": [{"voiceId": "v_nar", "confidence": 0.9, "reason": "适合旁白"}]}
    return {"recommendations": [{"voiceId": "v_hero", "confidence": 0.8, "reason": "冷峻克制"}]}


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
    )


def test_casting_handler_recommends_voices_and_stops_before_synthesis(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    _seed_voice(settings, "v_nar", "沉稳旁白", gender="女", usage_type=["旁白叙述"])
    _seed_voice(settings, "v_hero", "冷峻男声")
    jobs.enqueue(conn, "casting", book_id)

    assert run_once(_ctx(settings, conn, FakeLLM(routes={"【VOICE_RECOMMEND】": _route}))) is True

    casting = store.read_json(store.casting_path(settings, book_id))
    assert casting["roles"]["narrator"]["voice_id"] == "v_nar"
    assert casting["roles"]["role_0001"]["voice_id"] == "v_hero"
    assert casting["roles"]["role_0001"]["source"] == "llm"
    assert casting["roles"]["role_0001"]["recommendations"] == [
        {"voice_id": "v_hero", "voice_name": "冷峻男声", "confidence": 0.8, "reason": "冷峻克制"}
    ]
    assert casting["names"]["老苏"] == "role_0001"
    # 选角是分析链的最后一步：合成要等用户点「生成有声书」
    assert [j for j in jobs.list_jobs(conn, book_id) if j.kind == "synthesize"] == []


def test_casting_handler_keeps_a_manual_choice_but_refreshes_recommendations(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    _seed_voice(settings, "v_nar", "沉稳旁白")
    _seed_voice(settings, "v_hero", "冷峻男声")
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {"roles": {"role_0001": {"role_id": "role_0001", "voice_id": "v_hero", "source": "manual"}}},
    )
    jobs.enqueue(conn, "casting", book_id)

    def route(user: str) -> dict:
        if "角色：旁白" in user:
            return {"recommendations": [{"voiceId": "v_nar", "confidence": 0.9, "reason": "适合旁白"}]}
        return {"recommendations": [{"voiceId": "v_nar", "confidence": 0.7, "reason": "也行"}]}

    assert run_once(_ctx(settings, conn, FakeLLM(routes={"【VOICE_RECOMMEND】": route}))) is True

    role = store.read_json(store.casting_path(settings, book_id))["roles"]["role_0001"]
    assert role["voice_id"] == "v_hero" and role["source"] == "manual"
    assert role["recommendations"][0]["voice_id"] == "v_nar"


def test_casting_handler_without_voice_library_falls_back_and_records_issue(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    jobs.enqueue(conn, "casting", book_id)

    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", llm=None)
    assert run_once(ctx) is True

    casting = store.read_json(store.casting_path(settings, book_id))
    assert {role["voice_id"] for role in casting["roles"].values()} == {"default"}
    kinds = [issue["kind"] for issue in store.read_jsonl(store.issues_path(settings, book_id))]
    assert kinds == ["voice_library_empty"]
