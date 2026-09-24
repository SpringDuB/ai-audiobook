from audiobook import store


def test_paths_follow_layout(settings):
    assert store.book_dir(settings, "b1") == settings.books_dir / "b1"
    assert store.chapter_tag(1) == "0001"
    assert store.lines_path(settings, "b1", 1).name == "chapter_0001.jsonl"
    assert store.audio_dir(settings, "b1", 1).name == "chapter_0001"
    assert store.output_dir(settings, "b1").name == "output"


def test_atomic_write_replaces_and_leaves_no_tmp(settings):
    path = settings.data_dir / "x" / "a.txt"
    store.atomic_write_text(path, "第一版")
    store.atomic_write_text(path, "第二版")
    assert path.read_text(encoding="utf-8") == "第二版"
    assert list(path.parent.glob("*.tmp")) == []


def test_atomic_replace_json_roundtrip(settings):
    path = settings.data_dir / "b.json"
    store.atomic_replace_json(path, {"标题": "测试", "n": 1})
    assert store.read_json(path) == {"标题": "测试", "n": 1}
    assert store.read_json(path.with_name("missing.json"), default={"d": 1}) == {"d": 1}


def test_jsonl_write_and_append(settings):
    path = settings.data_dir / "rows.jsonl"
    store.write_jsonl_atomic(path, [{"id": "a"}, {"id": "b"}])
    store.append_jsonl(path, {"id": "c"})
    assert [r["id"] for r in store.read_jsonl(path)] == ["a", "b", "c"]
    assert path.read_text(encoding="utf-8").count("\n") == 3
