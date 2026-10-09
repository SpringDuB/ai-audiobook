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

from audiobook import jobs, store
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
            "roles": {
                "narrator": {
                    "role_id": "narrator",
                    "name": "旁白",
                    "voice_source": "design",
                    "source": "design",
                    "voice_id": "narrator",
                    "description": "三十多岁的男性，嗓音低沉厚实，语速中偏慢，讲述感强。",
                    "description_source": "llm",
                    "sample": "第一句。",
                    "recommendations": [],
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

    shelf = _probe(
        f"{served}/#/shelf",
        tmp_path / "shelf",
        extra=(
            "--eval=JSON.stringify({"
            " logo: (document.querySelector('.masthead__mark') || {}).naturalWidth || 0,"
            " favicon: (document.querySelector('link[rel=icon]') || {}).getAttribute('href') || '',"
            " })",
        ),
    )
    assert shelf["view"] == "shelf" and shelf["boot"] == "ready"
    assert shelf["consoleErrors"] == []
    assert shelf["counts"]["books"] == 1
    assert "冒烟书" in shelf["text"]
    brand = json.loads(next(value for key, value in shelf.items() if key.startswith("eval:")))
    assert brand["logo"] > 0                       # 品牌图真的加载出来了
    assert brand["favicon"] == "/static/logo-mark.png"

    workspace = _probe(f"{served}/#/book/{book_id}", tmp_path / "workspace")
    assert workspace["consoleErrors"] == []
    assert workspace["view"] == "book"
    assert workspace["counts"]["chapterItems"] == 1
    assert workspace["counts"]["castRows"] == 1
    assert "角色音色" in workspace["text"]
    # 角色行上是"音色描述 + 试听/保存/重写/绑库存音色"这一组动作
    assert workspace["counts"]["descEditors"] == 1
    assert workspace["counts"]["descActions"] == 4
    assert workspace["counts"]["bindButtons"] == 1
    assert "音色描述" in workspace["text"] or "三十多岁的男性" in workspace["text"]
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


def test_line_editor_uses_voice_prompt_not_emotion(served, settings, narrator_lines, tmp_path):
    """Qwen3 分支：角色文本不再展示情绪，编辑弹窗只改「本句表演描述」。"""
    book_id = _seed_book(settings, narrator_lines)
    rows = store.read_jsonl(store.lines_path(settings, book_id, 0))
    # 两行都给表演描述：第一行点开编辑后正文被替换，用第二行验证行内展示
    for row in rows:
        row["voice_prompt"] = "压低声音，语速放慢"
    store.write_jsonl_atomic(store.lines_path(settings, book_id, 0), rows)
    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "line-edit",
        extra=(
            "--click=.script__tools .tab:nth-child(2)",
            "--click=.line .line__tools button:nth-of-type(2)",
            "--eval=JSON.stringify({"
            " voice: (document.querySelector('.line__voice') || {}).textContent || '',"
            " labels: [...document.querySelectorAll('.line__edit label')].map((node) => node.textContent),"
            " toneTags: document.querySelectorAll('.tone-tag').length,"
            " selects: document.querySelectorAll('.line__edit select').length,"
            " })",
        ),
    )
    assert page["consoleErrors"] == []
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe["voice"] == "表演：压低声音，语速放慢"
    assert probe["labels"] == ["台词", "说话人", "本句表演描述"]
    assert probe["toneTags"] == 0        # 角色文本里不再有情绪标签
    assert probe["selects"] == 0         # 编辑弹窗里不再有情绪下拉


def test_workspace_voice_picker_lists_categories(served, settings, narrator_lines, tmp_path):
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)
    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "picker",
        extra=(
            "--click=.cast-row__bind",
            # 选择器是挂在 body 上的浮层，不在 #main 里，单独取它的文字
            "--eval=JSON.stringify({picker: (document.querySelector('.picker')?.innerText || '').replace(/\\s+/g, ' ')})",
        ),
    )
    assert page["consoleErrors"] == []
    assert page["counts"]["pickers"] == 1
    assert page["counts"]["pickerRows"] == 1
    picker = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert "测试男声" in picker["picker"]


def test_shelf_card_only_has_open_delete_and_opens_on_click(served, settings, narrator_lines, tmp_path):
    _seed_book(settings, narrator_lines)
    page = _probe(
        f"{served}/#/shelf",
        tmp_path / "shelf-actions",
        extra=(
            "--eval=JSON.stringify({"
            " actions: [...document.querySelectorAll('.book-card__actions .btn')].map((node) => node.textContent),"
            " importFile: Boolean(document.querySelector('.import input[type=file]')),"
            " importAlign: Math.round((document.querySelector('.import__row > .field') || {}).getBoundingClientRect"
            " ? document.querySelector('.import__row > .field').getBoundingClientRect().top"
            "   - document.querySelectorAll('.import__row > .field')[1].getBoundingClientRect().top : 0),"
            " })",
        ),
    )
    assert page["consoleErrors"] == []
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe["actions"] == ["打开", "删除"]          # 一键生成/异常按钮已下线
    assert probe["importFile"] is True                   # 导入区还收得到文件
    assert probe["importAlign"] == 0                     # 书稿/书名两列的标签顶在同一行

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
            " rerun: Boolean(document.querySelector('.switch-row')),"
            " text: (document.querySelector('.modal') || {}).innerText || '' })",
        ),
    )
    assert page["consoleErrors"] == []
    probe = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert probe["modal"] is True and probe["rows"] == 1
    # 默认跳过已分析好的章，返回的 force 由「覆盖重跑」开关决定
    assert probe["rerun"] is True
    assert "覆盖重跑" in probe["text"]
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


def _seed_synth_job(settings, book_id, *, chapter=0, done=1, total=2, inflight=("c0000-s01-l002",)):
    """插一条 running 的整章合成任务：progress 里点名"此刻在跑哪几行"。"""
    conn = connect(settings.db_path)
    init_db(conn)
    job_id = jobs.enqueue(conn, "synthesize", book_id, chapter)
    assert jobs.claim(conn, "smoke") is not None
    jobs.set_progress(conn, job_id, done, total, "c0000-s01-l001", extra={"inflight": list(inflight)})
    return job_id


_LINES_STATE = (
    "--eval=JSON.stringify({"
    " busy: document.querySelectorAll('.line__status--busy').length,"
    " statuses: [...document.querySelectorAll('.line__status')].map((node) => node.textContent.trim()),"
    " labels: [...document.querySelectorAll('.line')].map((row) =>"
    " (row.querySelector('.line__tools .btn') || {}).textContent.trim()),"
    " disabled: [...document.querySelectorAll('.line')].map((row) =>"
    " Boolean(row.querySelector('.line__tools .btn').disabled)),"
    " head: (document.querySelector('.script__meta') || {}).textContent || '',"
    " })"
)


def _seed_first_clip(settings, book_id, chapter=0):
    clip = store.audio_dir(settings, book_id, chapter) / "c0000-s01-l001.wav"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(wav_bytes())
    return clip


def test_workspace_marks_generating_line_with_hourglass(served, settings, narrator_lines, tmp_path):
    """正在生成的句子要挂转圈沙漏；还没轮到的是"排队中"，已完成的是可点的「试听」。"""
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)
    _seed_first_clip(settings, book_id)
    _seed_synth_job(settings, book_id)

    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "hourglass",
        extra=("--click=.script__tools .tab:nth-child(2)", _LINES_STATE),
    )
    assert page["consoleErrors"] == []
    state = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert state["busy"] == 1
    assert state["statuses"] == ["生成中"]
    assert state["labels"][:2] == ["试听", "未合成"]
    assert state["disabled"][:2] == [False, True]
    assert "已合成 1/2" in state["head"]


def test_workspace_picks_up_fresh_audio_without_reload(served, settings, narrator_lines, tmp_path):
    """第二句音频在页面打开后才落盘：角色文本要自己亮起来，不能等用户刷新。"""
    book_id = _seed_book(settings, narrator_lines)
    _seed_voice(settings)
    _seed_first_clip(settings, book_id)
    _seed_synth_job(settings, book_id)

    late = store.audio_dir(settings, book_id, 0) / "c0000-s01-l002.wav"

    def finish_later():
        time.sleep(2.0)
        late.write_bytes(wav_bytes())

    threading.Thread(target=finish_later, daemon=True).start()
    page = _probe(
        f"{served}/#/book/{book_id}",
        tmp_path / "live-flip",
        extra=("--click=.script__tools .tab:nth-child(2)", _LINES_STATE),
    )
    assert page["consoleErrors"] == []
    state = json.loads(next(value for key, value in page.items() if key.startswith("eval:")))
    assert state["busy"] == 0 and state["statuses"] == []     # 沙漏收了
    assert state["labels"][:2] == ["试听", "试听"]             # 新音频直接可点
    assert state["disabled"][:2] == [False, False]
    assert "已合成 2/2" in state["head"]
