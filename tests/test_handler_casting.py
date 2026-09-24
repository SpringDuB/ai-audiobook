from audiobook import jobs, store
from audiobook.handlers import casting as casting_handler  # noqa: F401
from audiobook.worker import WorkerContext, run_once


def _write_characters(settings, book_id: str) -> None:
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {
            "book_id": book_id,
            "generated_at": 0,
            "characters": [
                {"id": "narrator", "name": "旁白", "aliases": [], "gender": "未知", "age_group": "未知",
                 "personality": [], "speaking_style": "平稳", "base_emotion": "平静", "base_intensity": 0.3,
                 "chapters": [1], "mentions": 1, "is_narrator": True},
                {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年",
                 "personality": ["冷静"], "speaking_style": "平稳", "base_emotion": "平静", "base_intensity": 0.4,
                 "chapters": [1], "mentions": 2, "is_narrator": False},
            ],
            "relationships": [],
            "chapters": [],
            "dropped_relationships": 0,
        },
    )


def test_casting_handler_writes_casting_and_enqueues_synthesize(settings, conn):
    book_id = "book1"
    _write_characters(settings, book_id)
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": 1, "title": "第一章", "content": "第一句。", "chars": 4}]},
    )
    store.atomic_replace_json(store.scenes_path(settings, book_id, 1), {"scenes": []})
    store.write_jsonl_atomic(
        store.lines_path(settings, book_id, 1), [{"id": "c0001-s01-l001", "text": "第一句。"}]
    )
    jobs.enqueue(conn, "casting", book_id)

    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1")
    assert run_once(ctx) is True

    casting = store.read_json(store.casting_path(settings, book_id))
    assert casting["roles"]["role_0001"]["voice_id"] == "default"
    assert casting["names"]["老苏"] == "role_0001"
    synth = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "synthesize"]
    assert [j.chapter_index for j in synth] == [1]
    kinds = [issue["kind"] for issue in store.read_jsonl(store.issues_path(settings, book_id))]
    assert kinds == ["voice_library_empty"]
