from pathlib import Path

from .ffmpeg import run_ffmpeg


def write_media(
    settings,
    *,
    audio: Path,
    subtitles: Path | None,
    dst: Path,
    container: str = "mkv",
    title: str = "",
    ffmetadata: Path | None = None,
) -> None:
    """把 wav（+srt）封装成手机播放器能直接加载的 mkv/mp4，字幕是软字幕。"""
    fmt = (container or "mkv").lower()
    if fmt not in {"mkv", "mp4"}:
        raise ValueError(f"不支持的容器：{container}")
    args = ["-i", str(audio)]
    if subtitles is not None:
        args += ["-i", str(subtitles)]
    meta_index = None
    if ffmetadata is not None:
        meta_index = 2 if subtitles is not None else 1
        args += ["-f", "ffmetadata", "-i", str(ffmetadata)]
    args += ["-map", "0:a:0"]
    if subtitles is not None:
        args += ["-map", "1:0"]
    if meta_index is not None:
        args += ["-map_metadata", str(meta_index), "-map_chapters", str(meta_index)]
    if fmt == "mp4":
        args += ["-c:a", "aac", "-b:a", "128k"]
        if subtitles is not None:
            args += ["-c:s", "mov_text"]
        args += ["-movflags", "+faststart"]
    else:
        args += ["-c:a", "copy"]
        if subtitles is not None:
            args += ["-c:s", "srt"]
    if title:
        args += ["-metadata", f"title={title}"]
    if subtitles is not None:
        args += ["-metadata:s:s:0", "language=chi"]
    args.append(str(dst))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(settings, args)
