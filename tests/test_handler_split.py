from audiobook import jobs, store
from audiobook.handlers import split  # noqa: F401  导入即注册
from audiobook.importer import import_book
from audiobook.worker import WorkerContext, run_once


SAMPLE = "简介：一本书\n\n第一章 开始\n\n第一句。第二句。\n\n第二章 继续\n\n第三句。"


def test_import_then_split_enqueues_characters(conn, settings, tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="测试书")
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1")

    assert run_once(ctx) is True
    payload = store.read_json(store.chapters_path(settings, book_id))
    assert [c["title"] for c in payload["chapters"]] == ["前言", "第一章 开始", "第二章 继续"]
    assert payload["clean_stats"]["kept"] > 0

    queued = [j.kind for j in jobs.list_jobs(conn, book_id) if j.status == "queued"]
    assert queued == ["characters"]
    assert not store.lines_path(settings, book_id, 1).exists()


def test_import_copies_source_and_sets_book_id(conn, settings, tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    book_id = import_book(settings, conn, txt, title="测试书", book_id="fixed-id")
    assert book_id == "fixed-id"
    assert store.source_path(settings, book_id).read_text(encoding="utf-8") == SAMPLE
    assert store.read_json(store.book_dir(settings, book_id) / "book.json")["title"] == "测试书"
