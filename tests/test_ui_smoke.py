"""浏览器冒烟：真起服务 + headless Chrome 打开页面，校验渲染结果与 console 错误。

默认跳过（需要 Chrome + Node ≥ 22）：
    $env:AB_UI_SMOKE=1; uv run pytest tests/test_ui_smoke.py -q
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from audiobook import store
from audiobook.api.app import create_app
from audiobook.db import connect, init_db

CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AB_UI_SMOKE") != "1",
    reason="设置 AB_UI_SMOKE=1 才跑浏览器冒烟（需要 Chrome 与 Node ≥ 22）",
)


def _chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("找不到 Chrome，设置 CHROME_PATH 后重试")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def served(settings):
    port = _free_port()
    conn = connect(settings.db_path)
    init_db(conn)
    config = uvicorn.Config(create_app(settings, conn), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.1)
    else:  # pragma: no cover - 起不来就直接失败
        raise RuntimeError("uvicorn 未启动")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _probe(served_url: str, out_prefix: Path, width=1440, height=900, extra=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("找不到 node")
    env = {**os.environ, "CHROME_PATH": _chrome()}
    result = subprocess.run(
        [node, "tools/ui_probe.mjs", served_url, str(out_prefix), str(width), str(height), "--viewport", *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _seed_book(settings, narrator_lines, book_id="smoke"):
    conn = connect(settings.db_path)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (book_id, "冒烟书", "source/original.txt", 1, "imported", 1790000000000),
    )
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "冒烟书"})
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": 0, "title": "卷一", "content": "第一句。第二句。", "chars": 8}]},
    )
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), narrator_lines(0, "第一句。第二句。"))
    return book_id


def test_shelf_and_chapter_render_without_js_errors(served, settings, narrator_lines, tmp_path):
    settings = settings.model_copy(update={"export_mkv": False})
    book_id = _seed_book(settings, narrator_lines)

    shelf = _probe(f"{served}/#/shelf", tmp_path / "shelf")
    assert shelf["view"] == "shelf" and shelf["boot"] == "ready"
    assert shelf["consoleErrors"] == []
    assert shelf["counts"]["books"] == 1
    assert "冒烟书" in shelf["text"]

    chapter = _probe(f"{served}/#/book/{book_id}/chapter/0", tmp_path / "chapter", extra=("--scroll-to=.proof",))
    assert chapter["consoleErrors"] == []
    assert chapter["counts"]["proofLines"] == 2
    assert chapter["counts"]["seals"] >= 2
    assert "第一句。" in chapter["text"]


def test_settings_page_renders_all_groups(served, settings, tmp_path):
    page = _probe(f"{served}/#/settings", tmp_path / "settings")
    assert page["consoleErrors"] == []
    assert page["counts"]["fields"] >= 20
    assert "响度模式" in page["text"]


def test_mobile_viewport_has_bottom_rail(served, settings, tmp_path):
    page = _probe(
        f"{served}/#/shelf",
        tmp_path / "mobile",
        width=390,
        height=844,
        extra=(
            "--eval=JSON.stringify((() => { const r = document.querySelector('.rail').getBoundingClientRect();"
            " return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) }; })())",
        ),
    )
    assert page["consoleErrors"] == []
    rail = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert rail["bottom"] == 844 and rail["height"] < 120     # 固定在底部而不是铺满
