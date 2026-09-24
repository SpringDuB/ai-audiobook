import sys
import time
import os

from audiobook.config import get_settings, load_overlay
from audiobook.tts_service import LocalTtsService


def _sleeper(seconds: float = 30.0) -> list[str]:
    """跨平台的"占位进程"，用来当被托管的 TTS 服务。"""
    return [sys.executable, "-u", "-c", f"import sys,time; print('fake tts up', flush=True); time.sleep({seconds})"]


def _service(tmp_path, **overrides) -> LocalTtsService:
    settings = get_settings(data_dir=tmp_path / "data", **overrides)
    return LocalTtsService(settings, command=_sleeper())


def test_status_is_idle_before_any_start(tmp_path):
    service = _service(tmp_path)
    info = service.status()
    assert info["running"] is False
    assert info["pid"] is None
    assert info["url"] == "http://127.0.0.1:8020"


def test_start_stop_manages_child_process_and_state_file(tmp_path):
    service = _service(tmp_path)
    started = service.start(backend="indextts", port=8099)
    try:
        assert started["running"] is True
        assert started["pid"] and started["pid"] > 0
        assert started["port"] == 8099
        assert started["backend"] == "indextts"
        # 状态落在文件里，重启浏览器后还认得
        assert service.state_path.exists()
        assert service.status()["running"] is True
    finally:
        stopped = service.stop()
    assert stopped["running"] is False
    assert stopped["pid"] is None


def test_start_is_idempotent_when_already_running(tmp_path):
    service = _service(tmp_path)
    first = service.start(backend="indextts", port=8099)
    try:
        second = service.start(backend="indextts", port=8099)
        assert second["pid"] == first["pid"]
    finally:
        service.stop()


def test_start_restarts_when_running_backend_was_removed(tmp_path):
    """状态文件里是已删掉的 fake：一键启动要把它换掉，而不是"已经在跑"就返回。"""
    service = _service(tmp_path)
    first = service.start(backend="fake", port=8099)
    try:
        second = service.start(backend="indextts", port=8099)
        assert second["backend"] == "indextts"
        assert second["pid"] != first["pid"]
    finally:
        service.stop()


def test_logs_stream_incrementally(tmp_path):
    service = _service(tmp_path)
    service.start(backend="indextts", port=8099)
    try:
        first = service.logs(offset=0)
        assert first["reset"] is True
        assert any("启动 TTS" in line for line in first["lines"])
        second = service.logs(offset=first["offset"])
        assert second["reset"] is False
        assert second["offset"] >= first["offset"]
        assert all("启动 TTS" not in line for line in second["lines"])   # 不重复推旧内容
    finally:
        service.stop()


def test_dead_pid_is_not_reported_as_running(tmp_path):
    service = _service(tmp_path)
    service._write_state(pid=999999, port=9, backend="indextts", started_at=time.time())
    info = service.status()
    assert info["running"] is False
    assert info["pid"] is None
    assert service._read_state()["pid"] is None


def test_alive_pid_without_health_is_not_our_service(tmp_path):
    """pid 被系统回收给了别的进程：过了启动宽限期又不健康，就不能当成 TTS 在跑。"""
    from audiobook import tts_service

    service = _service(tmp_path, tts_port=9)
    service._write_state(pid=os.getpid(), port=9, backend="indextts", started_at=time.time() - 9999)
    info = service.status()
    assert info["running"] is False
    assert service._read_state()["pid"] is None
    assert tts_service.STARTUP_GRACE_SECONDS > 600      # 首次下载模型可能几十分钟，不能太早判死


def test_start_strips_parent_virtual_env(tmp_path, monkeypatch):
    """从根项目 .venv 里起后端时，VIRTUAL_ENV 会让 `uv run --project tts` 打警告。"""
    captured = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setenv("VIRTUAL_ENV", r"D:\workspace\ai-audiobook\.venv")
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "ai-audiobook")
    settings = get_settings(data_dir=tmp_path / "data")
    service = LocalTtsService(settings, command=_sleeper(), popen=fake_popen)
    service.start(backend="indextts", port=8099)
    assert "VIRTUAL_ENV" not in captured["env"]
    assert "VIRTUAL_ENV_PROMPT" not in captured["env"]
    assert captured["env"]["AIAB_TTS_BACKEND"] == "indextts"


def test_cli_start_writes_engine_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(LocalTtsService, "build_command", lambda self, *, backend, port: _sleeper())
    from audiobook import cli

    assert cli.main(["tts", "start", "--port", "8099"]) == 0
    settings = get_settings(data_dir=tmp_path / "data")
    service = LocalTtsService(settings)
    try:
        overlay = load_overlay(settings)
        assert overlay["engine"] == "http"
        assert overlay["tts_endpoints"] == ["http://127.0.0.1:8099"]
        assert service.status()["running"] is True
    finally:
        service.stop()
