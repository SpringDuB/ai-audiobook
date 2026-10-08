"""casting handler：登记角色 → voices/casting.json，并把缺描述的角色交给 voice_design。"""

from audiobook import jobs, store
from audiobook.handlers import casting as casting_handler  # noqa: F401
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


def _ctx(settings, conn, llm=None) -> WorkerContext:
    runner = LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings) if llm else None
    return WorkerContext(settings=settings, conn=conn, worker_id="w1", llm=runner)


def _next_jobs(conn, book_id: str, kind: str) -> list:
    return [job for job in jobs.list_jobs(conn, book_id) if job.kind == kind]


def test_casting_handler_registers_roles_then_asks_for_descriptions(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    _seed_voice(settings, "v_nar", "沉稳旁白")
    jobs.enqueue(conn, "casting", book_id)

    assert run_once(_ctx(settings, conn)) is True

    casting = store.read_json(store.casting_path(settings, book_id))
    assert casting["roles"]["role_0001"]["voice_source"] == "design"
    assert casting["roles"]["role_0001"]["voice_id"] == "role_0001"
    assert casting["roles"]["role_0001"]["description"] == ""
    assert casting["names"]["老苏"] == "role_0001"

    design_jobs = _next_jobs(conn, book_id, "voice_design")
    assert len(design_jobs) == 1
    # 不带 roles：任务自己算"谁缺描述"，多章各自登记一次会被幂等去重
    assert design_jobs[0].payload is None
    # 合成要等用户点「生成有声书」
    assert _next_jobs(conn, book_id, "synthesize") == []


def test_casting_handler_keeps_manual_library_binding_out_of_design(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    _seed_voice(settings, "v_hero", "冷峻男声")
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {"roles": {"role_0001": {"role_id": "role_0001", "voice_id": "v_hero", "source": "manual"}}},
    )
    jobs.enqueue(conn, "casting", book_id)

    assert run_once(_ctx(settings, conn)) is True

    role = store.read_json(store.casting_path(settings, book_id))["roles"]["role_0001"]
    assert role["voice_id"] == "v_hero"
    assert role["voice_source"] == "library"
    assert role["source"] == "manual"
    # 手工绑过的角色不需要写描述，但任务照样入队（描述生成时会跳过它）
    assert len(_next_jobs(conn, book_id, "voice_design")) == 1


def test_casting_handler_skips_roles_that_already_have_a_description(settings, conn):
    book_id = "book1"
    _seed_book(settings, book_id)
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "roles": {
                "role_0001": {
                    "role_id": "role_0001",
                    "source": "design",
                    "description": "二十出头的年轻男性，嗓音偏低。",
                    "description_source": "llm",
                }
            }
        },
    )
    jobs.enqueue(conn, "casting", book_id)

    assert run_once(_ctx(settings, conn)) is True

    role = store.read_json(store.casting_path(settings, book_id))["roles"]["role_0001"]
    assert role["description"].startswith("二十出头")
    assert len(_next_jobs(conn, book_id, "voice_design")) == 1


def test_scoped_casting_only_asks_for_this_chapters_roles(settings, conn):
    """单章分析：只给本章出现过的角色（+旁白）补描述，别把整本书重算一遍。"""
    book_id = "book1"
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {
            "book_id": book_id,
            "characters": [
                {"id": "narrator", "name": "旁白", "aliases": [], "chapters": [1], "mentions": 1, "is_narrator": True},
                {"id": "role_0001", "name": "苏锐", "aliases": [], "chapters": [1], "mentions": 1, "is_narrator": False},
                {"id": "role_0002", "name": "小鹿", "aliases": [], "chapters": [2], "mentions": 1, "is_narrator": False},
            ],
            "names": {"旁白": "narrator", "苏锐": "role_0001", "小鹿": "role_0002"},
        },
    )
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {
            "chapters": [
                {"index": 1, "title": "第一章", "content": "别废话。", "chars": 4},
                {"index": 2, "title": "第二章", "content": "走。", "chars": 2},
            ]
        },
    )
    store.write_jsonl_atomic(
        store.lines_path(settings, book_id, 1),
        [{"id": "c0001-s01-l001", "kind": "dialogue", "speaker": "role_0001", "speaker_name": "苏锐", "text": "别废话。"}],
    )
    store.write_jsonl_atomic(
        store.lines_path(settings, book_id, 2),
        [{"id": "c0002-s01-l001", "kind": "dialogue", "speaker": "role_0002", "speaker_name": "小鹿", "text": "走。"}],
    )
    jobs.enqueue(conn, "casting", book_id, payload={"chapters": [1]})

    assert run_once(_ctx(settings, conn)) is True

    casting = store.read_json(store.casting_path(settings, book_id))
    # 角色表照样登记全（避免幽灵角色），但描述只补本章的
    assert set(casting["roles"]) == {"narrator", "role_0001", "role_0002"}
    assert len(_next_jobs(conn, book_id, "voice_design")) == 1
