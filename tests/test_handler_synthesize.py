from audiobook import jobs, store
from fake_engine import FakeEngine
from audiobook.engines.errors import TtsVoiceMissing
from audiobook.handlers import synthesize  # noqa: F401  导入即注册
from audiobook.handlers.synthesize import effective_concurrency
from audiobook.worker import WorkerContext, run_once


class CountingEngine(FakeEngine):
    def __init__(self, **kwargs):
        super().__init__(ms_per_char=10.0, **kwargs)
        self.calls = 0

    def synthesize(self, text, voice_id, params, out_path):
        self.calls += 1
        return super().synthesize(text, voice_id, params, out_path)


class HintedEngine(FakeEngine):
    def __init__(self, hint: int):
        super().__init__(ms_per_char=10.0)
        self._hint = hint

    def concurrency_hint(self) -> int:
        return self._hint


class CapacityEngine(FakeEngine):
    """总容量 3，但此刻只剩 1 个空位（模拟服务端在忙）。"""

    def __init__(self, capacity: int):
        super().__init__(ms_per_char=10.0)
        self._capacity = capacity

    def capacity_hint(self) -> int:
        return self._capacity

    def concurrency_hint(self) -> int:
        return 1


class MissingRefEngine(FakeEngine):
    def __init__(self):
        super().__init__(ms_per_char=10.0)

    def synthesize(self, text, voice_id, params, out_path):
        if "第二句" in text:
            raise TtsVoiceMissing("缺少参考音频: data/voices/v_missing/ref.wav")
        return super().synthesize(text, voice_id, params, out_path)


def _prepare_book(narrator_lines, settings, book_id="b1", chapter=1, text="第一句。第二句。"):
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "T"})
    store.write_jsonl_atomic(store.lines_path(settings, book_id, chapter), narrator_lines(chapter, text))
    return book_id


def _run_synthesize(conn, ctx, book_id="b1", chapter=1):
    """只跑合成：先取消上一轮遗留的 post 任务，避免它抢在合成任务之前被领取。"""
    for job in jobs.list_jobs(conn, book_id):
        if job.kind == "post" and job.status == "queued":
            jobs.request_cancel(conn, job.id)
    jobs.enqueue(conn, "synthesize", book_id, chapter)
    run_once(ctx)


def test_synthesize_writes_clips_and_meta_then_enqueues_post(conn, settings, narrator_lines):
    engine = CountingEngine()
    _prepare_book(narrator_lines, settings)
    jobs.enqueue(conn, "synthesize", "b1", 1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)

    assert run_once(ctx) is True
    clips = sorted(store.audio_dir(settings, "b1", 1).glob("*.wav"))
    assert [c.name for c in clips] == ["c0001-s01-l001.wav", "c0001-s01-l002.wav"]
    meta = store.read_json(clips[0].with_suffix(".meta.json"))
    assert meta["engine"] == "fake"
    assert meta["cache_key"]
    assert meta["duration"] > 0
    assert [j.kind for j in jobs.list_jobs(conn, "b1")] == ["synthesize", "post"]


def test_second_run_reuses_cache_and_does_not_call_engine(conn, settings, narrator_lines):
    engine = CountingEngine()
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)

    jobs.enqueue(conn, "synthesize", "b1", 1)
    run_once(ctx)
    first_calls = engine.calls
    assert first_calls == 2

    _run_synthesize(conn, ctx)
    assert engine.calls == first_calls


def test_changed_text_regenerates_only_that_line(conn, settings, narrator_lines):
    engine = CountingEngine()
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)
    run_once(ctx)

    rows = store.read_jsonl(store.lines_path(settings, "b1", 1))
    rows[1]["text"] = "改过的第二句。"
    store.write_jsonl_atomic(store.lines_path(settings, "b1", 1), rows)
    before = engine.calls

    _run_synthesize(conn, ctx)
    assert engine.calls == before + 1


def test_failed_line_is_recorded_and_others_continue(conn, settings, narrator_lines):
    engine = CountingEngine(fail_on={"第二句"})
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)
    run_once(ctx)

    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.wav").exists()
    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert any(row["kind"] == "tts_line_failed" and row["line"] == "c0001-s01-l002" for row in issues)
    synth_job = [j for j in jobs.list_jobs(conn, "b1") if j.kind == "synthesize"][0]
    assert jobs.get_job(conn, synth_job.id).status == "done"


def test_effective_concurrency_prefers_engine_hint(settings):
    assert effective_concurrency(WorkerContext(settings=settings, conn=None, worker_id="w1", engine=HintedEngine(3))) == 3
    capped = settings.model_copy(update={"synth_concurrency_max": 2})
    assert effective_concurrency(WorkerContext(settings=capped, conn=None, worker_id="w1", engine=HintedEngine(3))) == 2
    assert effective_concurrency(
        WorkerContext(settings=settings, conn=None, worker_id="w1", engine=FakeEngine())
    ) == settings.synth_concurrency


def test_effective_concurrency_uses_total_capacity_not_free_slots(settings):
    """服务端总容量 3、此刻只剩 1 个空位时，整章仍要按 3 路起线程池。"""
    ctx = WorkerContext(settings=settings, conn=None, worker_id="w1", engine=CapacityEngine(3))
    assert effective_concurrency(ctx) == 3


def test_line_failure_is_recorded_with_tts_issue_kind(conn, settings, narrator_lines):
    engine = MissingRefEngine()
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)
    run_once(ctx)

    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert [issue["kind"] for issue in issues] == ["tts_ref_missing"]
    assert issues[0]["line"] == "c0001-s01-l002"
    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.wav").exists()
