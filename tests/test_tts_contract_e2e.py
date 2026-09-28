import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from audiobook import audio, jobs, store
from audiobook.db import connect, init_db
from audiobook.engines.factory import build_engine
from audiobook.handlers import casting, characters, lines, post, split, synthesize  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.pipeline import resume_book
from audiobook.worker import WorkerContext, run_once

REPO = Path(__file__).resolve().parents[1]
TTS_PROJECT = REPO / "tts"
TTS_TESTS = TTS_PROJECT / "tests"
UV = shutil.which("uv")

# 产品里的 TTS 服务只有 indextts（要 GPU + 权重），所以契约测试用 tts/tests 里的 stub 后端起一个真进程：
# 进程边界、HTTP 契约、参考音频上传、并发门这些要验的东西一个不少。
STUB_SERVER = (
    "import sys, uvicorn;"
    "sys.path.insert(0, r'{tests}');"
    "from _stub_backend import StubBackend;"
    "from aiab_tts.app import create_app;"
    "from aiab_tts.config import TtsSettings;"
    "from aiab_tts.state import ServiceState;"
    "s = TtsSettings(host='127.0.0.1', port={port});"
    "uvicorn.run(create_app(s, ServiceState(StubBackend(), s)), host='127.0.0.1', port={port}, log_level='warning')"
).replace("{tests}", str(TTS_TESTS))

SAMPLE = "第一章 开场\n\n苏锐说：“走。”\n\n王胖子说：“好。”"


def _route_extract(user: str) -> list[dict]:
    """提取假模型：归属句留在旁白，引语归到说话人。"""
    body = user.split("【正文】", 1)[-1]
    rows: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "说：“" in line:
            head, rest = line.split("说：“", 1)
            rows.append({"text": f"{head}说：", "role": "旁白", "emotion": None})
            rows.append({"text": f"“{rest}", "role": head, "emotion": "平静", "intensity": 0.4})
        else:
            rows.append({"text": line, "role": "旁白", "emotion": None})
    return rows


def _route_merge(user: str) -> dict:
    people = [{"name": name, "aliases": []} for name in ("苏锐", "王胖子") if name in user]
    return {"characters": people}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _service_command(port: int) -> list[str]:
    """优先直连 tts/.venv 的解释器：`uv run` 会多一层进程，kill 不干净会留下孤儿服务。"""
    code = STUB_SERVER.replace("{port}", str(port))
    python = TTS_PROJECT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        return [str(python), "-c", code]
    assert UV is not None
    return [UV, "run", "--project", str(TTS_PROJECT), "python", "-c", code]


def _kill_tree(process) -> None:
    """整棵进程树收尾（Windows 上用 taskkill /T），别留孤儿。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 收不干净就强杀
        process.kill()


def _wait_health(base_url: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=5.0)
            if response.status_code == 200:
                return response.json()
            last = f"{response.status_code} {response.text[:120]}"
        except Exception as exc:  # noqa: BLE001 - 启动期连不上是正常的
            last = str(exc)
        time.sleep(0.5)
    raise AssertionError(f"TTS 服务未在 {timeout}s 内就绪：{last}")


@pytest.mark.skipif(UV is None, reason="需要 uv 才能起 TTS 服务子进程")
@pytest.mark.skipif(os.environ.get("AB_SKIP_TTS_E2E") == "1", reason="AB_SKIP_TTS_E2E=1")
def test_chapter_synthesis_over_http_service(settings, tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        _service_command(port),
        cwd=str(REPO),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = _wait_health(base_url)
        assert health["engine"] == "stub-tts"
        assert health["recommendedConcurrency"] >= 1

        settings = settings.model_copy(
            update={"engine": "http", "tts_endpoints": [base_url], "synth_concurrency_max": 4}
        )
        # 音色参考音频：用最小 WAV 占位（stub 后端只校验文件存在）
        ref = settings.voices_dir / "default" / "ref.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"RIFFfake")

        conn = connect(settings.db_path)
        init_db(conn)
        txt = tmp_path / "book.txt"
        txt.write_text(SAMPLE, encoding="utf-8")
        book_id = import_book(settings, conn, txt, title="TTS 契约测试")

        llm = FakeLLM(routes={"【EXTRACT】": _route_extract, "【MERGE_ROLES】": _route_merge})
        engine = build_engine(settings)
        ctx = WorkerContext(
            settings=settings,
            conn=conn,
            worker_id="w1",
            engine=engine,
            llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
        )
        try:
            guard = 0
            while run_once(ctx):   # 导入 → 分章（本地，不碰 LLM）
                guard += 1
                assert guard < 100, "任务链没有收敛"
            resume_book(settings, conn, book_id, phase="analysis")   # 用户点「一键分析」
            while run_once(ctx):
                guard += 1
                assert guard < 100, "任务链没有收敛"
            resume_book(settings, conn, book_id, phase="audio")      # 用户点「生成有声书」
            while run_once(ctx):
                guard += 1
                assert guard < 100, "任务链没有收敛"
        finally:
            engine.close()

        out = store.output_dir(settings, book_id)
        assert (out / "chapter_0000.wav").exists()
        assert (out / "chapter_0000.srt").exists()
        assert audio.wav_duration(out / "chapter_0000.wav") > 0.2

        meta = store.read_json(store.audio_dir(settings, book_id, 0) / "c0000-s01-l001.meta.json")
        assert meta["engine"] == "stub-tts"
        assert meta["cache_key"]
        assert all(job.status == "done" for job in jobs.list_jobs(conn, book_id))
        # 合成阶段不允许出现任何行级异常；音色库未迁移时的 voice_library_empty 属预期
        kinds = {issue["kind"] for issue in store.read_jsonl(store.issues_path(settings, book_id))}
        assert kinds <= {"voice_library_empty"}
    finally:
        _kill_tree(process)
