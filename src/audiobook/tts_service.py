"""本机 TTS 推理服务的一键启动/停止。

TTS 仍然跑在独立进程（tts/ 子项目）里，这里只负责把它拉起来、看着它、按需停掉，
并把手头的进程信息落到 data/tts-service.json，方便重启浏览器后还能看到状态。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TTS_DIR = PROJECT_ROOT / "tts"

STILL_ACTIVE = 259
# 首次启动可能要先下几 GB 权重，宽限期给足；期间显示"启动中"，不再当成僵尸 pid
STARTUP_GRACE_SECONDS = 1800.0


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。Windows 上不能用 os.kill(pid, 0)——那会真的把进程杀掉。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台分支
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LocalTtsService:
    """管理 `uv run --project tts aiab-tts serve` 这个子进程。"""

    def __init__(self, settings, *, command: list[str] | None = None, popen=None):
        self.settings = settings
        self._command = command
        self._popen = popen or subprocess.Popen
        self._process = None

    # ---------------------------------------------------------------- 路径
    @property
    def state_path(self) -> Path:
        return Path(self.settings.data_dir) / "tts-service.json"

    @property
    def log_path(self) -> Path:
        return Path(self.settings.data_dir) / "logs" / "tts-service.log"

    # ---------------------------------------------------------------- 状态文件
    def _read_state(self) -> dict:
        path = self.state_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, **patch) -> dict:
        state = {**self._read_state(), **patch}
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return state

    # ---------------------------------------------------------------- 启动命令
    def build_command(self, *, backend: str, port: int) -> list[str]:
        if self._command:
            return list(self._command)
        args = ["serve", "--backend", backend, "--host", "127.0.0.1", "--port", str(port)]
        uv = shutil.which("uv")
        if uv:
            return [uv, "run", "--project", str(TTS_DIR), "aiab-tts", *args]
        python = TTS_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.exists():
            return [str(python), "-m", "aiab_tts", *args]
        raise RuntimeError("找不到 uv，且 tts/.venv 不存在：请先在项目根目录执行 uv sync 与 `uv run --project tts uv sync`")

    # ---------------------------------------------------------------- 探活
    def probe(self, url: str, timeout: float = 2.0) -> dict | None:
        try:
            import httpx

            response = httpx.get(f"{url.rstrip('/')}/health", timeout=timeout)
            if response.status_code != 200:
                return None
            payload = response.json()
        except Exception:  # noqa: BLE001 - 探活失败就是没起来
            return None
        return payload if isinstance(payload, dict) else None

    def status(self) -> dict:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        port = int(state.get("port") or self.settings.tts_port)
        url = f"http://127.0.0.1:{port}"
        started_at = state.get("started_at")
        fresh = bool(started_at) and (time.time() - float(started_at)) < STARTUP_GRACE_SECONDS
        alive = _pid_alive(pid)
        if pid and not alive:  # 进程已经没了：别拿着旧 pid 当"在跑"
            self._write_state(pid=None, stopped_pid=pid)
            pid = 0
        health = self.probe(url) if alive else None
        running = alive and (health is not None or fresh)
        if alive and not running:
            # 进程还在但既不健康也过了启动宽限期：多半是 pid 被系统回收了，别乱认
            self._write_state(pid=None, stale_pid=pid)
            pid = 0
        return {
            "running": running,
            "starting": running and health is None,
            "healthy": health is not None,
            "pid": pid or None,
            "port": port,
            "url": url,
            "backend": state.get("backend") or self.settings.tts_backend,
            "model_source": state.get("model_source") or self.settings.tts_model_source,
            "started_at": started_at,
            "health": health,
            "log_path": str(self.log_path),
        }

    # ---------------------------------------------------------------- 动作
    def start(
        self,
        *,
        backend: str | None = None,
        port: int | None = None,
        model_source: str | None = None,
        model_dir: str | None = None,
        hf_endpoint: str | None = None,
    ) -> dict:
        current = self.status()
        if current["running"]:
            return current
        backend = backend or self.settings.tts_backend
        port = int(port or self.settings.tts_port)
        model_source = model_source or self.settings.tts_model_source
        model_dir = model_dir or self.settings.tts_model_dir
        hf_endpoint = hf_endpoint if hf_endpoint is not None else self.settings.tts_hf_endpoint
        command = self.build_command(backend=backend, port=port)
        env = {
            **os.environ,
            "AIAB_TTS_BACKEND": backend,
            "AIAB_TTS_HOST": "127.0.0.1",
            "AIAB_TTS_PORT": str(port),
            "AIAB_TTS_MODEL_SOURCE": model_source,
            "AIAB_TTS_MODEL_DIR": str(model_dir),
        }
        # 后端进程常常是从根项目的 .venv 里起来的；这个变量会让 `uv run --project tts`
        # 报警说 VIRTUAL_ENV 和项目环境不匹配（其实会用 tts/.venv），去掉更干净
        env.pop("VIRTUAL_ENV", None)
        env.pop("VIRTUAL_ENV_PROMPT", None)
        if hf_endpoint:
            env["AIAB_TTS_HF_ENDPOINT"] = hf_endpoint
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} 启动 TTS：backend={backend} port={port} model={model_source} =====\n")
            handle.flush()
            self._process = self._popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        self._write_state(
            pid=self._process.pid,
            port=port,
            backend=backend,
            model_source=model_source,
            model_dir=str(model_dir),
            hf_endpoint=hf_endpoint,
            started_at=time.time(),
            command=command,
        )
        return self.status()

    def stop(self) -> dict:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        if pid and _pid_alive(pid):
            if os.name == "nt":  # pragma: no cover - 平台分支
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                import signal

                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        self._process = None
        self._write_state(pid=None, started_at=None, stopped_at=time.time())
        return self.status()

    def logs(self, *, offset: int = 0, limit: int = 300) -> dict:
        path = self.log_path
        if not path.exists():
            return {"offset": 0, "lines": [], "reset": bool(offset)}
        size = path.stat().st_size
        reset = offset <= 0 or offset > size
        start = 0 if reset else offset
        with open(path, "rb") as handle:
            handle.seek(start)
            chunk = handle.read()
        # 下载进度条是 \r 刷新的：换成换行，界面上就能一行行看到最新进度
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        trimmed = lines[-limit:] if len(lines) > limit else lines
        return {"offset": size, "lines": trimmed, "reset": reset}
