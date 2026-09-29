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
            "roles": {
                "narrator": {
                    "role_id": "narrator",
                    "name": "旁白",
                    "voice_id": "v1",
                    "voice_name": "测试男声",
                    "source": "llm",
                    "recommendations": [
                        {"voice_id": "v1", "voice_name": "测试男声", "confidence": 0.9, "reason": "叙述平稳"},
                        {"voice_id": "v2", "voice_name": "备选女声", "confidence": 0.5, "reason": "情绪更亮"},
                    ],
                }
            },
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
    assert workspace["counts"]["castRows"] == 1
    assert workspace["counts"]["recChips"] == 2   # 推荐音色直接排在角色行上
    assert "角色音色" in workspace["text"]
    assert "推荐" in workspace["text"]
    # 每个推荐音色都要有试听按钮（不是只能选中）
    assert workspace["counts"]["recPlays"] == workspace["counts"]["recChips"] == 2
    # 默认停在「原文」页签：进书先看干净原文，不再直接甩出角色文本
    assert workspace["counts"]["rawParagraphs"] >= 1
    assert workspace["counts"]["lines"] == 0
    assert "第一句。" in workspace["text"]

    # 顶栏：常驻三个主按钮 + 「更多」菜单，低频动作不挤在顶栏
    buttons = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "bar",
        extra=(
            "--eval=JSON.stringify({"
            " labels: [...document.querySelectorAll('.workbench__actions .btn')].map((node) => node.textContent),"
            " titles: [...document.querySelectorAll('.workbench__actions .btn')].map((node) => node.title),"
            " more: [...document.querySelectorAll('.workbench__more .menu .menu__item')].map((node) => node.textContent),"
            " moreTitle: (document.querySelector('.workbench__more > summary') || {}).title || '',"
            " bookWrap: getComputedStyle(document.querySelector('.workbench__book')).whiteSpace,"
            " })",
        ),
    )
    assert buttons["consoleErrors"] == []
    bar = json.loads(next(value for key, value in buttons.items() if key.startswith("eval:")))
    # 外部常驻四步：单章分析 / 单章音频 / 整本分析 / 整本音频
    assert bar["labels"] == ["分析本章台词", "生成本章音频", "分析全本台词", "生成整本音频"]
    assert all(bar["titles"]) and bar["moreTitle"]
    assert bar["more"] == ["导出整本成品", "打开成果文件夹", "重新拼接本章", "异常清单"]
    assert bar["bookWrap"] == "nowrap"          # 长书名永远单行 + 省略号

    # 切到「角色文本」页签，逐句标注要能直接看
    script = _probe(f"{served}/#/book/{book_id}", tmp_path / "script", extra=("--click=.script__tools .tab:nth-child(2)",))
    assert script["consoleErrors"] == []
    assert script["counts"]["lines"] == 2
    assert script["counts"]["seals"] >= 2   # 每句一枚钤印，只有角色文本里才有
    assert script["counts"]["rawParagraphs"] == 0
    assert "第一句。" in script["text"]


def test_workspace_voice_picker_lists_categories(served, settings, narrator_lines, tmp_path):
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)
    page = _probe(f"{served}/#/book/{book_id}", tmp_path / "picker", extra=("--click=.cast-row .btn",))
    assert page["consoleErrors"] == []
    assert page["counts"]["pickers"] == 1
    assert page["counts"]["pickerRows"] == 1
    assert "测试男声" in page["text"]


def test_shelf_card_only_has_open_delete_and_opens_on_click(served, settings, narrator_lines, tmp_path):
    _seed_book(settings, narrator_lines)
    page = _probe(
        f"{served}/#/shelf",
        tmp_path / "shelf-actions",
        extra=(
            "--eval=JSON.stringify([...document.querySelectorAll('.book-card__actions .btn')]"
            ".map((node) => node.textContent))",
        ),
    )
    assert page["consoleErrors"] == []
    actions = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert actions == ["打开", "删除"]          # 一键生成/异常按钮已下线

    # 点卡片正文（不是链接、不是按钮）也要能进书页
    clicked = _probe(f"{served}/#/shelf", tmp_path / "shelf-card-click", extra=("--click=.book-card__stats",))
    assert clicked["consoleErrors"] == []
    assert clicked["view"] == "book"


def test_voices_page_uploads_and_has_no_casting_matrix(served, settings, tmp_path):
    _seed_voice(settings)
    page = _probe(
        f"{served}/#/voices",
        tmp_path / "voices",
        extra=(
            "--eval=JSON.stringify({"
            ' upload: Boolean(document.querySelector(".voice-upload")),'
            ' tables: document.querySelectorAll(".table").length,'
            ' fileInput: Boolean(document.querySelector(".voice-upload input[type=file]")),'
            "})",
        ),
    )
    assert page["consoleErrors"] == []
    assert "上传新音色" in page["text"] and "加入音色库" in page["text"]
    assert page["counts"]["voices"] == 1
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe == {"upload": True, "tables": 0, "fileInput": True}   # 角色 → 音色矩阵已移除


def test_analyze_button_opens_chapter_picker(served, settings, narrator_lines, tmp_path):
    """「分析角色文本」要弹章节多选窗，而不是直接整书重跑。"""
    book_id = _seed_book(settings, narrator_lines)
    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "analyze-picker",
        extra=(
            "--click=.workbench__actions .btn:nth-of-type(3)",   # 分析全本台词
            "--eval=JSON.stringify({ modal: Boolean(document.querySelector('.modal--wide')),"
            " rows: document.querySelectorAll('.pick-row').length,"
            " text: (document.querySelector('.modal') || {}).innerText || '' })",
        ),
    )
    assert page["consoleErrors"] == []
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe["modal"] is True and probe["rows"] == 1
    assert "卷一" in probe["text"]


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


def test_theme_boot_and_toggle_persists(served, settings, tmp_path):
    """首访落一个主题（跟随系统），点切换后翻转并写进 localStorage。"""
    boot_page = _probe(
        f"{served}/#/shelf",
        tmp_path / "theme-boot",
        extra=(
            "--eval=JSON.stringify({"
            " theme: document.documentElement.dataset.theme,"
            " scheme: getComputedStyle(document.documentElement).colorScheme,"
            " label: document.getElementById('theme-toggle').getAttribute('aria-label'),"
            " })",
        ),
    )
    assert boot_page["consoleErrors"] == []
    boot = json.loads(next(value for key, value in boot_page.items() if key.startswith("eval:")))
    assert boot["theme"] in {"light", "dark"}
    assert boot["scheme"] == boot["theme"]              # 原生控件跟随主题
    assert boot["label"].startswith("切换到")

    toggled_page = _probe(
        f"{served}/#/shelf",
        tmp_path / "theme-toggle",
        extra=(
            "--click=#theme-toggle",
            "--eval=JSON.stringify({"
            " theme: document.documentElement.dataset.theme,"
            " saved: localStorage.getItem('aiab-theme'),"
            " meta: document.querySelector('meta[name=\"theme-color\"]').content,"
            " bg: getComputedStyle(document.body).backgroundColor,"
            " })",
        ),
    )
    assert toggled_page["consoleErrors"] == []
    toggled = json.loads(next(value for key, value in toggled_page.items() if key.startswith("eval:")))
    assert toggled["theme"] != boot["theme"]
    assert toggled["saved"] == toggled["theme"]
    assert toggled["meta"] == ("#12100e" if toggled["theme"] == "dark" else "#f7f4ef")


def test_lines_are_colored_by_role(served, settings, narrator_lines, tmp_path):
    """角色文本里每句都带角色配色槽位；旁白保持中性。"""
    book_id = _seed_book(settings, narrator_lines)
    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "role-hues",
        extra=(
            "--click=.script__tools .tab:nth-child(2)",
            "--eval=JSON.stringify({"
            " hues: [...document.querySelectorAll('.line')].map((row) => row.dataset.hue),"
            " who: (document.querySelector('.line__who') || {}).textContent || '',"
            " bg: getComputedStyle(document.querySelector('.line__who')).backgroundColor,"
            " })",
        ),
    )
    assert page["consoleErrors"] == []
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe["hues"] == ["narrator", "narrator"]
    assert probe["who"] == "旁白"
    assert probe["bg"] != "rgba(0, 0, 0, 0)"


def test_open_output_folder_button_follows_export(served, settings, narrator_lines, tmp_path):
    """「打开成果文件夹」在导出前禁用，产物出现后可用。"""
    book_id = _seed_book(settings, narrator_lines)
    find = (
        "--eval=JSON.stringify((() => {"
        " const node = [...document.querySelectorAll('.workbench__more .menu__item')]"
        ".find((item) => item.textContent === '打开成果文件夹');"
        " return { found: Boolean(node), disabled: Boolean(node && node.disabled), title: (node || {}).title || '' };"
        " })())"
    )

    before = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "output-off",
        extra=("--click=.workbench__more > summary", find),
    )
    assert before["consoleErrors"] == []
    off = json.loads(next(value for key, value in before.items() if key.startswith("eval:")))
    assert off["found"] is True and off["disabled"] is True
    assert "先点" in off["title"]

    out = store.output_dir(settings, book_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "book.wav").write_bytes(b"RIFF0000WAVE")

    after = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "output-on",
        extra=("--click=.workbench__more > summary", find),
    )
    assert after["consoleErrors"] == []
    on = json.loads(next(value for key, value in after.items() if key.startswith("eval:")))
    assert on["disabled"] is False
