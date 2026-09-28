from audiobook import jobs, store
from fake_engine import FakeEngine
from audiobook.handlers import post, synthesize, synthesize_line  # noqa: F401  导入即注册
from audiobook.worker import WorkerContext, run_once


def _seed(settings, narrator_lines, chapter=1):
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"), {"chapters": [{"index": chapter, "title": "一", "content": "x"}]}
    )
    rows = narrator_lines(chapter, "第一句。第二句。")
    store.write_jsonl_atomic(store.lines_path(settings, "b1", chapter), rows)
    engine = FakeEngine(ms_per_char=10.0)
    for row in rows:
        engine.synthesize(row["text"], "default", None, store.audio_dir(settings, "b1", chapter) / f"{row['id']}.wav")
    return rows


def test_synthesize_line_touches_only_target_line(conn, settings, narrator_lines):
    _seed(settings, narrator_lines)
    target = store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav"
    other = store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.wav"
    # 比对内容而不是 mtime：这台机器上重写文件有时不改 mtime（同秒内重写），断言会假失败
    before = (target.read_bytes(), other.read_bytes())
    jobs.enqueue_line(conn, "b1", 1, "c0001-s01-l002")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine(ms_per_char=20.0))
    while run_once(ctx):
        pass
    assert target.read_bytes() != before[0]                # 目标行用新引擎（20ms/字）重合成
    assert other.read_bytes() == before[1]                 # 其它行不动
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM jobs ORDER BY id")]
    assert kinds == ["synthesize_line", "post"]
    statuses = {row["kind"]: row["status"] for row in conn.execute("SELECT kind, status FROM jobs")}
    assert statuses == {"synthesize_line": "done", "post": "done"}


def test_synthesize_line_reports_missing_chapter(conn, settings):
    jobs.enqueue_line(conn, "b1", 0, "c0000-s01-l001", max_attempts=1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    row = conn.execute("SELECT status, error FROM jobs WHERE kind='synthesize_line'").fetchone()
    assert row["status"] == "failed" and "没有行数据" in row["error"]


def test_synthesize_line_reports_unknown_line(conn, settings, narrator_lines):
    _seed(settings, narrator_lines)
    jobs.enqueue_line(conn, "b1", 1, "c0001-s99-l404", max_attempts=1)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=FakeEngine())
    while run_once(ctx):
        pass
    row = conn.execute("SELECT status, error FROM jobs WHERE kind='synthesize_line'").fetchone()
    assert row["status"] == "failed" and "不在第 1 章" in row["error"]
