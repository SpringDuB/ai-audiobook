import threading
import time
from dataclasses import replace

from audiobook import jobs, store
from fake_engine import FakeEngine
from audiobook.engines.errors import TtsVoiceMissing
from audiobook.handlers import synthesize  # noqa: F401  导入即注册
from audiobook.handlers.synthesize import effective_concurrency, group_batches
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


class GatedEngine(FakeEngine):
    """第二句卡住不返回：用来观察"另一行还在跑"时任务进度里的 inflight。"""

    def __init__(self):
        super().__init__(ms_per_char=1.0)
        self.entered = threading.Event()
        self.release = threading.Event()

    def synthesize(self, text, voice_id, params, out_path):
        if "第二句" in text:
            self.entered.set()
            self.release.wait(timeout=15.0)
        return super().synthesize(text, voice_id, params, out_path)


class BatchEngine(FakeEngine):
    """支持批量合成的假引擎：记录每包几条，失败可注入（用来测退回逐行）。"""

    def __init__(self, batch_limit: int = 4, fail_batch: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.batch_limit = batch_limit
        self.fail_batch = fail_batch
        self.batches: list[int] = []

    def capabilities(self):
        return replace(super().capabilities(), batch=True, max_batch_items=self.batch_limit)

    def synthesize_batch(self, items, voice_id, out_paths):
        self.batches.append(len(items))
        if self.fail_batch:
            raise RuntimeError("批量注入失败")
        results = []
        for (text, params), path in zip(items, out_paths):
            results.append(super().synthesize(text, voice_id, params, path))
        return results


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


def test_description_change_regenerates_only_that_role(conn, settings, narrator_lines):
    """改某个角色的音色描述：只重合成这个角色的句子，别的句子继续走缓存。"""
    rows = narrator_lines(1, "第一句。第二句。")
    rows[1]["speaker"] = "role_0001"
    rows[1]["speaker_name"] = "小鹿"
    _prepare_book(narrator_lines, settings)
    store.write_jsonl_atomic(store.lines_path(settings, "b1", 1), rows)

    def casting(description: str) -> dict:
        return {
            "roles": {
                "role_0001": {
                    "role_id": "role_0001",
                    "voice_source": "design",
                    "source": "design",
                    "description": description,
                }
            },
            "names": {"小鹿": "role_0001"},
        }

    store.atomic_replace_json(
        store.casting_path(settings, "b1"),
        casting("二十出头的年轻女性，声音清脆，语速偏快。"),
    )
    engine = CountingEngine()
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)

    _run_synthesize(conn, ctx)
    assert engine.calls == 2

    # 小鹿换音色描述 → 只重跑小鹿那一句；旁白那句已经有音频，不再找引擎
    store.atomic_replace_json(
        store.casting_path(settings, "b1"),
        casting("三十多岁的女性，嗓音低哑，语速缓慢。"),
    )
    before = engine.calls
    _run_synthesize(conn, ctx)
    assert engine.calls == before + 1
    meta = store.read_json(store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.meta.json")
    assert meta["voice_source"] == "design"
    assert meta["voice_key"].startswith("design:role_0001:")

    # 已经按新描述合成过：再点一次一个字都不重跑
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


def test_progress_reports_which_lines_are_running(conn, settings, narrator_lines):
    """整章合成要能把"此刻在跑哪几行"报出来（前端靠它画沙漏、实时点亮试听）。"""
    engine = GatedEngine()
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    job_id = jobs.enqueue(conn, "synthesize", "b1", 1)

    worker = threading.Thread(target=lambda: run_once(ctx), daemon=True)
    worker.start()
    try:
        assert engine.entered.wait(timeout=15.0), "第二句没跑到引擎"
        inflight = []
        deadline = time.time() + 15.0
        while time.time() < deadline:
            progress = jobs.get_job(conn, job_id).progress or {}
            if progress.get("done", 0) >= 1:
                inflight = list(progress.get("inflight") or [])
                break
            time.sleep(0.05)
        # 第一句已完成、第二句还卡在引擎里 → 只有它还该被标成"生成中"
        assert inflight == ["c0001-s01-l002"]
    finally:
        engine.release.set()
        worker.join(timeout=20.0)

    assert jobs.get_job(conn, job_id).status == "done"
    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").exists()


def test_group_batches_packs_same_voice_and_keeps_long_text_alone():
    """同音色的短句要打进同一包；太长的文本（客户端本来要分块）单独一包。"""
    rows = [
        {"id": "a", "text": "短句。"},
        {"id": "b", "text": "很长" * 200},
        {"id": "c", "text": "另一句。"},
        {"id": "d", "text": "第三句。"},
    ]
    targets = {row["id"]: {"voice_id": "v1"} for row in rows}
    groups = group_batches(rows, targets, 4, 300)
    assert [[row["id"] for row in group] for group in groups] == [["b"], ["a", "c", "d"]]


def test_group_batches_sorts_by_length_to_avoid_padding_waste():
    """一个包会被补齐到最长那条：长短混排会白算，所以要按字数排序再打包。"""
    rows = [
        {"id": "long", "text": "长" * 30},
        {"id": "mid", "text": "中" * 12},
        {"id": "short", "text": "短"},
    ]
    targets = {row["id"]: {"voice_id": "v1"} for row in rows}
    groups = group_batches(rows, targets, 3, 300)
    assert [[row["id"] for row in group] for group in groups] == [["short", "mid", "long"]]


def test_synthesize_packs_same_voice_into_one_batch(conn, settings, narrator_lines):
    """整章合成时同音色的行要打包成一次批量解码（不再一行一个请求）。"""
    engine = BatchEngine()
    _prepare_book(narrator_lines, settings)
    settings = settings.model_copy(update={"synth_batch_size": 2, "synth_batch_workers": 1})
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)

    assert run_once(ctx) is True
    assert engine.batches == [2]                    # 两句一包，一次解码
    clips = sorted(store.audio_dir(settings, "b1", 1).glob("*.wav"))
    assert [clip.name for clip in clips] == ["c0001-s01-l001.wav", "c0001-s01-l002.wav"]
    meta = store.read_json(clips[0].with_suffix(".meta.json"))
    assert meta["duration"] > 0 and meta["engine"] == "fake"


def test_batch_failure_falls_back_to_per_line(conn, settings, narrator_lines):
    """批量失败不能把整章拖垮：自动退回逐行，音频照样产出。"""
    engine = BatchEngine(fail_batch=True)
    _prepare_book(narrator_lines, settings)
    settings = settings.model_copy(update={"synth_batch_size": 2, "synth_batch_workers": 1})
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)

    assert run_once(ctx) is True
    assert engine.batches == [2]                    # 试过一次批量，失败了
    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.wav").exists()
    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").exists()
    assert store.read_jsonl(store.issues_path(settings, "b1")) == []
