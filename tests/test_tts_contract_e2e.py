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
from audiobook.handlers import casting, characters, lines, post, scenes, split, synthesize  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

REPO = Path(__file__).resolve().parents[1]
TTS_PROJECT = REPO / "tts"
UV = shutil.which("uv")

SAMPLE = "第一章 开场\n\n苏锐说：“走。”\n\n王胖子说：“好。”"
PASS_A = {
    "characters": [
        {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年"},
        {"name": "王胖子", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [],
}
PASS_B = {"scenes": [{"index": 1, "title": "开场", "participants": ["苏锐", "王胖子"], "tone": "平静"}]}


def _route_c(user: str) -> dict:
    tail = user.split("句子列表：", 1)[-1]
    sentences = [line.split(". ", 1)[1] for line in tail.splitlines() if ". " in line and line[:1].isdigit()]
    rows = []
    for position, sentence in enumerate(sentences, start=1):
        if "苏锐" in sentence:
            speaker = "苏锐"
        elif "王胖子" in sentence:
            speaker = "王胖子"
        else:
            speaker = "旁白"
        rows.append({"index": position, "speaker": speaker, "emotion": "平静", "intensity": 0.4})
    return {"lines": rows}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        [UV, "run", "--project", str(TTS_PROJECT), "python", "-m", "aiab_tts",
         "serve", "--backend", "fake", "--port", str(port)],
        cwd=str(REPO),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = _wait_health(base_url)
        assert health["engine"] == "fake-tts"
        assert health["recommendedConcurrency"] >= 1

        settings = settings.model_copy(
            update={"engine": "http", "tts_endpoints": [base_url], "synth_concurrency_max": 4}
        )
        # 音色参考音频：M6 迁移前用最小 WAV 占位（fake 后端只校验文件存在）
        ref = settings.voices_dir / "default" / "ref.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"RIFFfake")

        conn = connect(settings.db_path)
        init_db(conn)
        txt = tmp_path / "book.txt"
        txt.write_text(SAMPLE, encoding="utf-8")
        book_id = import_book(settings, conn, txt, title="TTS 契约测试")

        llm = FakeLLM(routes={"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": _route_c})
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
        assert meta["engine"] == "fake-tts"
        assert meta["cache_key"]
        assert all(job.status == "done" for job in jobs.list_jobs(conn, book_id))
        # 合成阶段不允许出现任何行级异常；音色库未迁移时的 voice_library_empty 属预期
        kinds = {issue["kind"] for issue in store.read_jsonl(store.issues_path(settings, book_id))}
        assert kinds <= {"voice_library_empty"}
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
