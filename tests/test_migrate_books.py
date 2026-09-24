import json

from audiobook import store
from audiobook.migrate.books import compare_book_roles, migrate_book


def test_migrate_book_copies_cover_legacy_and_writes_report(settings, conn, tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("第一章 起风\n\n第一句。第二句。\n", encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "chapters.json").write_text(
        json.dumps([{"index": 0, "title": "第一章 起风", "content": "第一句。第二句。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (legacy / "roles_0.json").write_text(
        json.dumps([{"text": "第一句。", "role": "旁白"}], ensure_ascii=False), encoding="utf-8"
    )
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n")

    report = migrate_book(settings, conn, txt, title="样本书", legacy_dir=legacy, cover=cover, book_id="mig1")
    book_dir = store.book_dir(settings, "mig1")
    assert (book_dir / "cover.png").read_bytes() == cover.read_bytes()
    assert (book_dir / "legacy" / "roles_0.json").exists()
    assert (book_dir / "legacy" / "chapters.json").exists()
    assert report["book_id"] == "mig1"
    assert report["source"]["sha256"] and report["source"]["bytes"] == txt.stat().st_size
    assert report["chapters"]["new_count"] == 1
    assert report["legacy"]["roles_files"] == ["roles_0.json"]
    assert store.read_json(book_dir / "migration.json")["book_id"] == "mig1"
    assert store.read_json(book_dir / "book.json")["cover"] == "cover.png"
    row = conn.execute("SELECT title FROM books WHERE id='mig1'").fetchone()
    assert row["title"] == "样本书"


def test_migrate_book_without_legacy_or_cover(settings, conn, tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("第一章 起风\n\n第一句。\n", encoding="utf-8")
    report = migrate_book(settings, conn, txt, title="光板", book_id="mig2")
    assert report["legacy"]["roles_files"] == []
    assert report["chapters"]["old_count"] == 0
    assert not (store.book_dir(settings, "mig2") / "cover.png").exists()


def test_compare_book_roles_updates_migration_report(settings, conn, tmp_path, narrator_lines):
    txt = tmp_path / "book.txt"
    txt.write_text("第一章 起风\n\n第一句。第二句。\n", encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "roles_0.json").write_text(
        json.dumps(
            [{"text": "第一句。", "role": "旁白"}, {"text": "第二句。", "role": "张卫东"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    migrate_book(settings, conn, txt, title="对照书", legacy_dir=legacy, book_id="mig3")
    rows = narrator_lines(0, "第一句。第二句。")
    rows[1]["speaker_name"] = "张卫东"          # 模拟新系统分析结果
    store.write_jsonl_atomic(store.lines_path(settings, "mig3", 0), rows)

    result = compare_book_roles(settings, "mig3", legacy)
    assert (result["old_lines"], result["matched"], result["agree"]) == (2, 2, 2)
    saved = store.read_json(store.book_dir(settings, "mig3") / "migration.json")
    assert saved["roles_compare"]["agreement_rate"] == 1.0
