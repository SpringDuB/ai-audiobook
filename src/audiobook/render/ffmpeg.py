import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    channels: int
    bits: int
    duration: float
    frames: int


def _probe_name(path: str) -> str:
    return "ffprobe.exe" if path.lower().endswith(".exe") else "ffprobe"


def _sibling(binary: str, name: str) -> str | None:
    candidate = Path(binary).with_name(name)
    return str(candidate) if candidate.exists() else None


def find_ffmpeg(explicit: str = "") -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise FFmpegError(f"AB_FFMPEG_PATH 指向的文件不存在：{explicit}")
    found = shutil.which("ffmpeg")
    if not found:
        raise FFmpegError("找不到 ffmpeg：请安装后加入 PATH，或用 AB_FFMPEG_PATH 指定可执行文件")
    return found


def find_ffprobe(explicit: str = "") -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            sibling = _sibling(str(path), _probe_name(str(path)))
            if sibling:
                return sibling
            raise FFmpegError(f"找不到与 {explicit} 同目录的 ffprobe")
    found = shutil.which("ffprobe")
    if not found:
        raise FFmpegError("找不到 ffprobe：请安装后加入 PATH，或用 AB_FFMPEG_PATH 指定 ffmpeg")
    return found


def run_ffmpeg(settings, args: list[str], *, loglevel: str = "error", timeout: float | None = None) -> str:
    binary = find_ffmpeg(settings.ffmpeg_path)
    cmd = [binary, "-hide_banner", "-nostdin", "-loglevel", loglevel, "-y", *args]
    limit = timeout if timeout is not None else settings.ffmpeg_timeout_seconds
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=limit, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg 超时（{limit}s）：{args[0] if args else ''}") from exc
    except FileNotFoundError as exc:  # pragma: no cover - 取决于机器
        raise FFmpegError(f"无法启动 ffmpeg：{exc}") from exc
    output = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-15:])
        raise FFmpegError(f"ffmpeg 退出码 {proc.returncode}：\n{tail}")
    return output


def probe_wav(path: Path) -> AudioInfo:
    with wave.open(str(path)) as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        return AudioInfo(
            sample_rate=rate,
            channels=handle.getnchannels(),
            bits=handle.getsampwidth() * 8,
            duration=frames / float(rate) if rate else 0.0,
            frames=frames,
        )


def probe_json(settings, path: Path) -> dict:
    binary = find_ffprobe(settings.ffmpeg_path)
    cmd = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 退出码 {proc.returncode}：{proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")
