"""lines handler：把提取结果物化成行记录，缺提取就先补一次。"""

from audiobook import jobs, store
from audiobook.analysis.readiness import casting_ready
from audiobook.handlers import lines as lines_handler  # noqa: F401
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

CHARACTERS = {
    "book_id": "b1",
    "characters": [
        {"id": "narrator", "name": "旁白", "aliases": [], "chapters": [], "mentions": 0, "is_narrator": True},
        {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "chapters": [1], "mentions": 1, "is_narrator": False},
    ],
    "names": {"旁白": "narrator", "苏锐": "role_0001", "老苏": "role_0001"},
}

EXTRACTION = {
    "windows": 1,
    "lines": [
        {"text": "苏锐说：", "role": "旁白", "emotion": None},
        {"text": "“走。”", "role": "苏锐", "emotion": "愤怒", "intensity": 0.7},
    ],
}


def _seed_chapter(settings, book_id="b1", index=1, content="苏锐说：“走。”") -> None:
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": index, "title": "第一章", "content": content, "chars": len(content)}]},
    )


def _ctx(settings, conn, llm) -> WorkerContext:
    return WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings) if llm else None,
    )


def test_lines_handler_materializes_extraction_and_triggers_casting(settings, conn):
    book_id = "b1"
    _seed_chapter(settings, book_id)
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    store.atomic_replace_json(store.extract_path(settings, book_id, 1), EXTRACTION)
    jobs.enqueue(conn, "lines", book_id, 1)

    assert run_once(_ctx(settings, conn, None)) is True

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    assert [row["kind"] for row in rows] == ["narration", "dialogue"]
    assert [row["speaker"] for row in rows] == ["narrator", "role_0001"]
    assert [row["id"] for row in rows] == ["c0001-s01-l001", "c0001-s01-l002"]
    assert rows[1]["emotion"]["dominant"] == "愤怒"
    assert rows[1]["emotion"]["mix"] == [{"name": "愤怒", "weight": 0.7}]
    # 旁白不带情绪向量
    assert rows[0]["emotion"] == {"dominant": "平静", "intensity": 0.0, "source": "none"}
    casting_jobs = [j for j in jobs.list_jobs(conn, book_id) if j.kind == "casting"]
    assert len(casting_jobs) == 1
    assert casting_ready(settings, conn, book_id) is True


def test_lines_handler_merges_a_new_name_into_the_character_table(settings, conn):
    book_id = "b1"
    _seed_chapter(settings, book_id)
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    store.atomic_replace_json(
        store.extract_path(settings, book_id, 1),
        {"windows": 1, "lines": [{"text": "锐哥，走。", "role": "锐哥", "emotion": "平静", "intensity": 0.3}]},
    )
    jobs.enqueue(conn, "lines", book_id, 1)
    llm = FakeLLM(routes={"【MERGE_ROLES】": {"characters": [{"name": "苏锐", "aliases": ["锐哥"]}]}})

    assert run_once(_ctx(settings, conn, llm)) is True

    rows = store.read_jsonl(store.lines_path(settings, book_id, 1))
    assert rows[0]["speaker"] == "role_0001"
    assert "【MERGE_ROLES】" in llm.calls[0]["user"]
    assert store.read_json(store.characters_path(settings, book_id))["names"]["锐哥"] == "role_0001"


def test_lines_handler_reruns_extraction_only_when_it_is_missing(settings, conn):
    """「分析本章」会删掉本章提取结果 → lines 自己补跑一次 LLM。"""
    book_id = "b1"
    _seed_chapter(settings, book_id)
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    jobs.enqueue(conn, "lines", book_id, 1)

    def route(user: str) -> list[dict]:
        return [
            {"text": "苏锐说：", "role": "旁白", "emotion": None},
            {"text": "“走。”", "role": "苏锐", "emotion": "平静", "intensity": 0.3},
        ]

    llm = FakeLLM(routes={"【EXTRACT】": route})
    assert run_once(_ctx(settings, conn, llm)) is True

    assert store.read_json(store.extract_path(settings, book_id, 1))["lines"]
    assert store.read_jsonl(store.lines_path(settings, book_id, 1))


def test_casting_not_ready_while_analysis_jobs_are_active(settings, conn):
    book_id = "b1"
    _seed_chapter(settings, book_id)
    jobs.enqueue(conn, "lines", book_id, 1)
    assert casting_ready(settings, conn, book_id) is False


def test_lines_handler_invalidates_chapter_audio_when_annotation_changes(settings, conn):
    """重分析改了标注 → 本章与整本成品作废，等「生成有声书」按新标注重建。"""
    book_id = "b1"
    _seed_chapter(settings, book_id)
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    store.atomic_replace_json(store.extract_path(settings, book_id, 1), EXTRACTION)
    store.atomic_write_bytes(store.chapter_wav_path(settings, book_id, 1), b"RIFF")
    store.atomic_write_bytes(store.chapter_srt_path(settings, book_id, 1), b"1\n")
    store.atomic_replace_json(store.chapter_render_meta_path(settings, book_id, 1), {"render_version": 2})
    store.atomic_write_bytes(store.book_wav_path(settings, book_id), b"RIFF")
    jobs.enqueue(conn, "lines", book_id, 1)

    assert run_once(_ctx(settings, conn, None)) is True

    assert not store.chapter_wav_path(settings, book_id, 1).exists()
    assert not store.chapter_srt_path(settings, book_id, 1).exists()
    assert not store.chapter_render_meta_path(settings, book_id, 1).exists()
    assert not store.book_wav_path(settings, book_id).exists()


def test_lines_handler_keeps_audio_when_annotation_is_unchanged(settings, conn):
    """标注重跑但内容没变（例如重试落盘任务）→ 不动已合成的成品。"""
    book_id = "b1"
    _seed_chapter(settings, book_id)
    store.atomic_replace_json(store.characters_path(settings, book_id), CHARACTERS)
    store.atomic_replace_json(store.extract_path(settings, book_id, 1), EXTRACTION)
    jobs.enqueue(conn, "lines", book_id, 1)
    assert run_once(_ctx(settings, conn, None)) is True
    store.atomic_write_bytes(store.chapter_wav_path(settings, book_id, 1), b"RIFF")

    jobs.enqueue(conn, "lines", book_id, 1)
    assert run_once(_ctx(settings, conn, None)) is True

    assert store.chapter_wav_path(settings, book_id, 1).exists()
