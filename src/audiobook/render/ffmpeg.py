import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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


def _vendor_binary(name: str) -> str | None:
    """项目内 vendor/ffmpeg/bin 下自带的二进制（放了整包构建时优先用）。"""
    for candidate in (f"{name}.exe", name):
        path = PROJECT_ROOT / "vendor" / "ffmpeg" / "bin" / candidate
        if path.is_file():
            return str(path)
    return None


def bundled_ffmpeg() -> str | None:
    """项目自带的 ffmpeg：优先 vendor/，其次 imageio-ffmpeg 依赖随包分发的静态构建。"""
    vendored = _vendor_binary("ffmpeg")
    if vendored:
        return vendored
    try:
        import imageio_ffmpeg
    except ImportError:  # pragma: no cover - 依赖缺失时退回 PATH
        return None
    try:
        candidate = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 取不到就交给 PATH
        return None
    return candidate if candidate and Path(candidate).is_file() else None


def find_ffmpeg(explicit: str = "") -> str:
    """按 显式路径 → 项目自带 → PATH 的顺序找 ffmpeg（用户不需要自己配）。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise FFmpegError(f"AB_FFMPEG_PATH 指向的文件不存在：{explicit}")
    bundled = bundled_ffmpeg()
    if bundled:
        return bundled
    found = shutil.which("ffmpeg")
    if not found:
        raise FFmpegError("找不到 ffmpeg：项目自带的依赖缺失，请先 `uv sync`（或把 ffmpeg 装进 PATH）")
    return found


def find_ffprobe(explicit: str = "") -> str | None:
    """ffprobe 是可选的：没有就走 probe_json 里的 `ffmpeg -i` 解析兜底。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            sibling = _sibling(str(path), _probe_name(str(path)))
            if sibling:
                return sibling
            if "ffprobe" in path.name.lower():
                return str(path)
        else:
            raise FFmpegError(f"AB_FFMPEG_PATH 指向的文件不存在：{explicit}")
    vendored = _vendor_binary("ffprobe")
    if vendored:
        return vendored
    return shutil.which("ffprobe")


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
    if binary:
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
        if proc.returncode == 0:
            return json.loads(proc.stdout or "{}")
    return probe_with_ffmpeg(settings, path)


def probe_with_ffmpeg(settings, path: Path) -> dict:
    """没有 ffprobe 时的兜底：解析 `ffmpeg -i` 的输出（字段只保证我们导出校验用到的部分）。"""
    binary = find_ffmpeg(settings.ffmpeg_path)
    proc = subprocess.run(
        [binary, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    if "Input #0" not in text:
        raise FFmpegError(f"读不出媒体信息：{Path(path).name}")
    return parse_ffmpeg_info(text)


_STREAM_TYPES = {
    "Video": "video",
    "Audio": "audio",
    "Subtitle": "subtitle",
    "Data": "data",
    "Attachment": "attachment",
}


def parse_ffmpeg_info(text: str) -> dict:
    """把 `ffmpeg -i` 的文本输出整理成与 ffprobe JSON 同形的子集。"""
    fmt = {"format_name": "", "duration": "", "nb_streams": 0}
    streams: list[dict] = []
    chapters: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Input #0"):
            after = line.split("Input #0", 1)[1].lstrip(", ").strip()
            fmt["format_name"] = after.split(", from", 1)[0].strip().rstrip(":")
        elif line.startswith("Duration:"):
            seconds = _parse_duration(line.split("Duration:", 1)[1].split(",", 1)[0].strip())
            if seconds is not None:
                fmt["duration"] = f"{seconds:.3f}"
        elif line.startswith("Stream #"):
            stream = _parse_stream(line)
            if stream is not None:
                streams.append(stream)
        elif line.startswith("Chapter #"):
            chapter = _parse_chapter(line)
            if chapter is not None:
                chapters.append(chapter)
        elif line.startswith("title") and ":" in line and chapters and "title" not in chapters[-1]["tags"]:
            chapters[-1]["tags"]["title"] = line.split(":", 1)[1].strip()
    fmt["nb_streams"] = len(streams)
    return {"streams": streams, "format": fmt, "chapters": chapters}


def _parse_duration(value: str) -> float | None:
    try:
        parts = [float(part) for part in value.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:  # 兼容 HH:MM:SS.ms 与 MM:SS.ms
        seconds = seconds * 60 + part
    return seconds


def _parse_stream(line: str) -> dict | None:
    spec, _, rest = line[len("Stream #") :].partition(": ")
    kind, _, body = rest.partition(":")
    codec_type = _STREAM_TYPES.get(kind.strip())
    if codec_type is None:
        return None
    parts = [part.strip() for part in body.split(",")]
    stream: dict = {
        "index": int(spec.split(":", 1)[0] or 0),
        "codec_type": codec_type,
        "codec_name": (parts[0] if parts else "").split(" ")[0],
    }
    if "(" in spec:
        language = spec.split("(", 1)[1].split(")", 1)[0].strip()
        if language:
            stream["tags"] = {"language": language}
    for part in parts[1:]:
        if part.endswith(" Hz"):
            stream["sample_rate"] = int(part.split(" ", 1)[0])
        elif part == "mono":
            stream["channels"] = 1
        elif part == "stereo":
            stream["channels"] = 2
        elif part.endswith(" channels"):
            stream["channels"] = int(part.split(" ", 1)[0])
        elif part == "default":
            stream["disposition"] = {"default": 1}
    return stream


def _parse_chapter(line: str) -> dict | None:
    body = line[len("Chapter #") :]
    try:
        start = float(body.split("start", 1)[1].split(",", 1)[0].strip())
        end = float(body.split("end", 1)[1].split(",", 1)[0].strip())
    except (IndexError, ValueError):
        return None
    return {"start_time": start, "end_time": end, "tags": {}}
