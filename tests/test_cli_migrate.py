import json

from audiobook.cli import main
from test_migrate_voices import _fake_library


def test_cli_migrate_voices_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    source = _fake_library(tmp_path)
    assert main(["migrate", "voices", "--source", str(source), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "3 个音色" in out and "待补标签" in out
    assert not (tmp_path / "data" / "voices").exists()


def test_cli_migrate_voices_real(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    source = _fake_library(tmp_path)
    assert main(["migrate", "voices", "--source", str(source)]) == 0
    out = capsys.readouterr().out
    assert "v001 … v003" in out
    assert (tmp_path / "data" / "voices" / "v001" / "ref.wav").exists()


def test_cli_migrate_voices_missing_source_returns_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    assert main(["migrate", "voices", "--source", str(tmp_path / "nope")]) == 2
    assert "参数错误" in capsys.readouterr().out


def test_cli_migrate_book_and_compare(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AB_DATA_DIR", str(data_dir))
    txt = tmp_path / "book.txt"
    txt.write_text("第一章 起风\n\n第一句。第二句。\n", encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "chapters.json").write_text(
        json.dumps([{"index": 0, "title": "第一章 起风", "content": "第一句。第二句。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (legacy / "roles_0.json").write_text(
        json.dumps([{"text": "第一句。", "role": "旁白"}], ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["migrate", "book", str(txt), "--title", "样本书", "--legacy", str(legacy), "--book-id", "mig1"]) == 0
    out = capsys.readouterr().out
    assert "book_id: mig1" in out and "标题命中 1" in out

    # 造一份分析结果，再跑对照
    from audiobook import store
    from audiobook.config import get_settings

    settings = get_settings()
    store.write_jsonl_atomic(
        store.lines_path(settings, "mig1", 0),
        [
            {"id": "c0000-s01-l001", "text": "第一句。", "speaker": "narrator", "speaker_name": "旁白"},
            {"id": "c0000-s01-l002", "text": "第二句。", "speaker": "narrator", "speaker_name": "旁白"},
        ],
    )
    assert main(["compare", "mig1", "--legacy", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "一致率：100.0%" in out
