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
from helpers import wav_bytes

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
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {"characters": [{"id": "narrator", "name": "旁白"}]},
    )
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "narrator_voice": "v1",
            "roles": {"narrator": {"role_id": "narrator", "name": "旁白", "voice_id": "v1", "voice_name": "测试男声"}},
        },
    )
    return book_id


def _seed_voice(settings, voice_id="v1", name="测试男声"):
    path = settings.voices_dir / voice_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "ref.wav").write_bytes(wav_bytes())
    store.atomic_replace_json(
        path / "voice.json",
        {
            "id": voice_id,
            "name": name,
            "gender": "男",
            "age_group": "青年",
            "usage_type": ["角色对话"],
            "personality": ["沉稳"],
            "description": "测试用音色",
        },
    )
    return voice_id


def test_shelf_and_workspace_render_without_js_errors(served, settings, narrator_lines, tmp_path):
    settings = settings.model_copy(update={"export_mkv": False})
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)

    shelf = _probe(f"{served}/#/shelf", tmp_path / "shelf")
    assert shelf["view"] == "shelf" and shelf["boot"] == "ready"
    assert shelf["consoleErrors"] == []
    assert shelf["counts"]["books"] == 1
    assert "冒烟书" in shelf["text"]

    workspace = _probe(f"{served}/#/book/{book_id}", tmp_path / "workspace")
    assert workspace["consoleErrors"] == []
    assert workspace["view"] == "book"
    assert workspace["counts"]["chapterItems"] == 1
    assert workspace["counts"]["lines"] == 2
    assert workspace["counts"]["castRows"] == 1
    assert workspace["counts"]["seals"] >= 2
    assert "第一句。" in workspace["text"]
    assert "角色音色" in workspace["text"]

    # 切到「原文」页签，原文要能直接看
    raw = _probe(f"{served}/#/book/{book_id}", tmp_path / "raw", extra=("--click=.script__tools .tab:nth-child(2)",))
    assert raw["consoleErrors"] == []
    assert raw["counts"]["lines"] == 0
    assert raw["counts"]["rawParagraphs"] >= 1


def test_workspace_voice_picker_lists_categories(served, settings, narrator_lines, tmp_path):
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)
    page = _probe(f"{served}/#/book/{book_id}", tmp_path / "picker", extra=("--click=.cast-row .btn",))
    assert page["consoleErrors"] == []
    assert page["counts"]["pickers"] == 1
    assert page["counts"]["pickerRows"] == 1
    assert "测试男声" in page["text"]


def test_shelf_delete_book_asks_then_removes(served, settings, narrator_lines, tmp_path):
    book_id = _seed_book(settings, narrator_lines)

    opened = _probe(
        f"{served}/#/shelf",
        tmp_path / "delete-ask",
        extra=(
            "--click=.book-card .btn-danger",
            "--eval=JSON.stringify({ modal: Boolean(document.querySelector('.modal')),"
            " text: (document.querySelector('.modal') || {}).innerText || '' })",
        ),
    )
    assert opened["consoleErrors"] == []
    probe = json.loads(next(value for key, value in opened.items() if key.startswith("eval:")))
    assert probe["modal"] is True and "删除这本书" in probe["text"]
    assert store.book_dir(settings, book_id).exists()  # 只是弹窗，还没删

    deleted = _probe(
        f"{served}/#/shelf",
        tmp_path / "delete-confirm",
        extra=(
            "--click=.book-card .btn-danger",
            "--click=.modal__actions .btn-danger",
            "--wait=1500",
            "--eval=JSON.stringify({ cards: document.querySelectorAll('.book-card').length,"
            " modal: Boolean(document.querySelector('.modal')) })",
        ),
    )
    assert deleted["consoleErrors"] == []
    probe = json.loads(next(value for key, value in deleted.items() if key.startswith("eval:")))
    assert probe == {"cards": 0, "modal": False}
    assert not store.book_dir(settings, book_id).exists()


def test_settings_offers_one_click_tts_and_hides_low_level_knobs(served, settings, tmp_path):
    page = _probe(
        f"{served}/#/settings",
        tmp_path / "settings",
        extra=(
            "--eval=JSON.stringify({"
            'loudness: Boolean(document.querySelector("[data-key=loudness_mode]")),'
            'ffmpeg: Boolean(document.querySelector("[data-key=ffmpeg_path]")),'
            'launch: Boolean(document.querySelector("#f-tts_backend")),'
            'emotion: Boolean(document.querySelector("#f-emotion_mode")),'
            "})",
        ),
    )
    assert page["consoleErrors"] == []
    assert page["counts"]["fields"] >= 10
    assert "一键启动 TTS 服务" in page["text"]
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    # emotion=False：文本描述情绪通道暂时关闭，设置页不出现这个下拉
    assert probe == {"loudness": False, "ffmpeg": False, "launch": True, "emotion": False}


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
