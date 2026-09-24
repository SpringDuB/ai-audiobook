import shutil

import pytest

from audiobook import store
from audiobook.migrate.snapshot import export_snapshot, import_snapshot


def _seed(settings, narrator_lines, book_id="b1"):
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "快照书"})
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": 0, "title": "卷一", "content": "第一句。", "chars": 4}]},
    )
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), narrator_lines(0, "第一句。第二句。"))
    store.atomic_replace_json(store.casting_path(settings, book_id), {"book_id": book_id, "narrator_voice": "v001", "roles": {}})
    return book_id


def test_snapshot_round_trip(settings, conn, narrator_lines, tmp_path):
    book_id = _seed(settings, narrator_lines)
    out = tmp_path / "snap.zip"
    manifest = export_snapshot(settings, book_id, out)
    assert manifest["book_id"] == book_id and manifest["files"] >= 4 and out.exists()
    assert all(name.endswith((".json", ".jsonl")) for name in manifest["names"])

    shutil.rmtree(store.book_dir(settings, book_id))
    restored = import_snapshot(settings, conn, out)
    assert restored["restored"] == manifest["files"]
    assert store.read_json(store.book_dir(settings, book_id) / "book.json")["title"] == "快照书"
    assert len(store.read_jsonl(store.lines_path(settings, book_id, 0))) == 2
    with pytest.raises(FileExistsError):
        import_snapshot(settings, conn, out)
    assert import_snapshot(settings, conn, out, force=True)["restored"] >= 4


def test_snapshot_import_can_rename_book(settings, conn, narrator_lines, tmp_path):
    _seed(settings, narrator_lines)
    out = tmp_path / "snap.zip"
    export_snapshot(settings, "b1", out)
    result = import_snapshot(settings, conn, out, book_id="copy1")
    assert result["book_id"] == "copy1"
    assert store.read_json(store.book_dir(settings, "copy1") / "book.json")["id"] == "copy1"
    assert store.read_json(store.casting_path(settings, "copy1"))["book_id"] == "copy1"
    assert conn.execute("SELECT title FROM books WHERE id='copy1'").fetchone()["title"] == "快照书"


def test_snapshot_excludes_audio_and_reports_hashes(settings, conn, narrator_lines, tmp_path):
    _seed(settings, narrator_lines)
    store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", 0), b"RIFF")
    store.atomic_write_bytes(store.audio_dir(settings, "b1", 0) / "c0000-s01-l001.wav", b"RIFF")
    manifest = export_snapshot(settings, "b1", tmp_path / "s.zip")
    assert all(not name.endswith((".wav", ".mp3", ".mkv")) for name in manifest["names"])
    assert all(len(entry["sha256"]) == 64 for entry in manifest["entries"])


def test_snapshot_missing_book_raises(settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        export_snapshot(settings, "nope", tmp_path / "x.zip")
