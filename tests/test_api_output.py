"""导出产物的列出与「打开成果文件夹」。"""

from audiobook import store

from test_api_read import _client


def _book(settings, book_id="output-book"):
    store.book_dir(settings, book_id).mkdir(parents=True, exist_ok=True)
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "产物书"})
    return book_id


def test_book_payload_lists_output_files(settings):
    client = _client(settings)
    book_id = _book(settings)

    payload = client.get(f"/api/books/{book_id}").json()
    assert payload["output"]["exists"] is False
    assert payload["output"]["files"] == []

    out = store.output_dir(settings, book_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "book.wav").write_bytes(b"RIFF0000WAVE")
    (out / "book.srt").write_text("1\n", encoding="utf-8")

    payload = client.get(f"/api/books/{book_id}").json()
    assert payload["output"]["exists"] is True
    assert [item["name"] for item in payload["output"]["files"]] == ["book.srt", "book.wav"]
    assert payload["output"]["files"][1]["size"] == 12

    listing = client.get(f"/api/books/{book_id}/output").json()
    assert listing["exists"] is True and listing["dir"].endswith("output")


def test_reveal_output_opens_folder_only_when_ready(settings, monkeypatch):
    client = _client(settings)
    book_id = _book(settings)

    # 还没导出 → 404，界面据此保持按钮禁用
    assert client.post(f"/api/books/{book_id}/output/reveal").status_code == 404

    opened: list[str] = []
    monkeypatch.setattr("audiobook.api.app._reveal_directory", lambda path: opened.append(str(path)) or True)

    out = store.output_dir(settings, book_id)
    out.mkdir(parents=True, exist_ok=True)
    response = client.post(f"/api/books/{book_id}/output/reveal")
    assert response.status_code == 200
    assert response.json()["opened"] is True
    assert opened == [str(out)]

    assert client.post("/api/books/not-here/output/reveal").status_code == 404


def test_reveal_reports_failure_without_raising(settings, monkeypatch):
    client = _client(settings)
    book_id = _book(settings)
    store.output_dir(settings, book_id).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("audiobook.api.app._reveal_directory", lambda path: False)

    body = client.post(f"/api/books/{book_id}/output/reveal").json()
    assert body["opened"] is False          # 打不开不算服务端错误，界面自己提示
