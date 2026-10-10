"""手机听书：把章节 wav 按需转成低码率 m4a、给出逐句时间轴与听书目录。

章节 wav 是 44.1kHz 无损，一章动辄 80~100MB，手机流量和离线缓存都扛不住；
这里用 ffmpeg 转一份 AAC 单声道（默认 64kbps，约 wav 的 1/8），落进
`output/mobile/chapter_XXXX.m4a`。转码是懒加载 + 按 mtime 失效：章节重渲染后
自动重转，没变的章节直接命中缓存。

字幕时间轴直接读渲染产物 `chapter_XXXX.srt`（它由真实片段时长累计而来），
再和逐句标注对齐，补上说话人；文本对不上的（旧产物 / 人工改过台词）不硬认人。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from . import store
from .render import srt as srt_mod
from .render.ffmpeg import FFmpegError, run_ffmpeg

MOBILE_DIRNAME = "mobile"

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def mobile_dir(settings, book_id: str) -> Path:
    return store.output_dir(settings, book_id) / MOBILE_DIRNAME


def mobile_audio_path(settings, book_id: str, index: int) -> Path:
    return mobile_dir(settings, book_id) / f"chapter_{store.chapter_tag(index)}.m4a"


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _is_fresh(m4a: Path, wav: Path) -> bool:
    try:
        return m4a.stat().st_size > 0 and m4a.stat().st_mtime >= wav.stat().st_mtime
    except OSError:
        return False


def ensure_mobile_audio(settings, book_id: str, index: int, *, force: bool = False) -> Path:
    """章节 wav → m4a（已有且不比 wav 旧就直接复用）。"""
    wav = store.chapter_wav_path(settings, book_id, index)
    if not wav.exists():
        raise FileNotFoundError("本章还没有生成音频，先跑「生成本章音频」")
    dst = mobile_audio_path(settings, book_id, index)
    if not force and _is_fresh(dst, wav):
        return dst
    with _lock_for(str(dst)):
        if not force and _is_fresh(dst, wav):
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.part")
        try:
            encoder = str(getattr(settings, "mobile_audio_encoder", "aac") or "aac").strip() or "aac"
            args = ["-i", str(wav), "-vn", "-map_metadata", "-1", "-c:a", encoder]
            if encoder == "aac":
                # profile 只有内置 aac 认；塞给 aac_mf 会直接失败
                args += ["-profile:a", "aac_low"]
            args += [
                "-b:a", f"{int(settings.mobile_audio_bitrate_kbps)}k",
                "-ac", str(int(settings.mobile_audio_channels)),
                "-movflags", "+faststart",
                "-f", "mp4",
                str(tmp),
            ]
            run_ffmpeg(settings, args)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    return dst


def _chapter_title(settings, book_id: str, index: int) -> str:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    for chapter in chapters:
        if int(chapter.get("index", -1)) == index:
            return str(chapter.get("title") or f"第 {index} 章")
    return f"第 {index} 章"


def _book_title(settings, book_id: str) -> str:
    meta = store.read_json(store.book_dir(settings, book_id) / "book.json", default={}) or {}
    return str(meta.get("title") or book_id)


def _speaker_per_cue(settings, book_id: str, index: int, cues: list) -> list[dict]:
    """按渲染顺序对齐逐句标注：只有句子里有音频的才会出现在 SRT 里。"""
    rows = store.read_jsonl(store.lines_path(settings, book_id, index))
    clips_dir = store.audio_dir(settings, book_id, index)
    speakers: list[dict] = []
    for row in rows:
        if len(speakers) >= len(cues):
            break
        if not (clips_dir / f"{row['id']}.wav").exists():
            continue
        cue = cues[len(speakers)]
        if (cue.text or "").strip() == (row.get("text") or "").strip():
            speakers.append(
                {
                    "speaker": row.get("speaker"),
                    "speaker_name": row.get("speaker_name") or row.get("speaker"),
                }
            )
        else:
            speakers.append({})
    while len(speakers) < len(cues):
        speakers.append({})
    return speakers


def chapter_timeline(settings, book_id: str, index: int) -> dict:
    """一章的播放时间轴：逐句 start/end/文本/说话人 + 音频地址。"""
    srt_path = store.chapter_srt_path(settings, book_id, index)
    if not srt_path.exists():
        raise FileNotFoundError("本章还没有字幕，先跑「生成本章音频」")
    cues = srt_mod.parse_srt(srt_path)
    speakers = _speaker_per_cue(settings, book_id, index, cues)
    meta = store.read_json(store.chapter_render_meta_path(settings, book_id, index), default={}) or {}
    duration = float(meta.get("duration") or 0.0)
    if not duration and cues:
        duration = float(cues[-1].end)
    m4a = mobile_audio_path(settings, book_id, index)
    return {
        "book_id": book_id,
        "book_title": _book_title(settings, book_id),
        "index": index,
        "title": _chapter_title(settings, book_id, index),
        "duration": round(duration, 3),
        "cues": [
            {
                "i": i,
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "text": cue.text,
                "speaker": who.get("speaker"),
                "speaker_name": who.get("speaker_name"),
            }
            for i, (cue, who) in enumerate(zip(cues, speakers), start=1)
        ],
        "audio": {
            "m4a_url": f"/api/books/{book_id}/chapters/{index}/audio.m4a",
            "wav_url": f"/api/books/{book_id}/chapters/{index}/audio",
            "m4a_ready": m4a.exists(),
            "m4a_bytes": m4a.stat().st_size if m4a.exists() else None,
            "m4a_mtime": round(m4a.stat().st_mtime, 3) if m4a.exists() else None,
            "bitrate_kbps": int(settings.mobile_audio_bitrate_kbps),
        },
    }


def _estimate_bytes(duration: float, bitrate_kbps: int) -> int:
    return int(max(duration, 0.0) * bitrate_kbps * 1000 / 8)


def catalog(settings, book_id: str) -> dict:
    """听书目录：只列已经有成品的章节（wav + srt 都在）。"""
    book_dir = store.book_dir(settings, book_id)
    if not book_dir.exists():
        raise FileNotFoundError("book not found")
    channels = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    entries: list[dict] = []
    for chapter in channels:
        index = int(chapter.get("index", -1))
        if index < 0:
            continue
        wav = store.chapter_wav_path(settings, book_id, index)
        srt_path = store.chapter_srt_path(settings, book_id, index)
        if not (wav.exists() and srt_path.exists()):
            continue
        meta = store.read_json(store.chapter_render_meta_path(settings, book_id, index), default={}) or {}
        duration = float(meta.get("duration") or 0.0)
        cue_count = int(meta.get("cues") or 0)
        if not duration or not cue_count:
            cues = srt_mod.parse_srt(srt_path)
            cue_count = cue_count or len(cues)
            duration = duration or (float(cues[-1].end) if cues else 0.0)
        m4a = mobile_audio_path(settings, book_id, index)
        audio_mtime = max(wav.stat().st_mtime, srt_path.stat().st_mtime)
        entries.append(
            {
                "index": index,
                "title": str(chapter.get("title") or f"第 {index} 章"),
                "duration": round(duration, 3),
                "cues": cue_count,
                "chars": int(chapter.get("chars") or 0),
                "wav_bytes": wav.stat().st_size,
                "m4a_ready": m4a.exists() and m4a.stat().st_mtime >= audio_mtime,
                "m4a_bytes": m4a.stat().st_size if m4a.exists() else None,
                "m4a_mtime": round(m4a.stat().st_mtime, 3) if m4a.exists() else None,
                "estimated_bytes": _estimate_bytes(duration, int(settings.mobile_audio_bitrate_kbps)),
                "audio_mtime": round(audio_mtime, 3),
            }
        )
    return {
        "book_id": book_id,
        "title": _book_title(settings, book_id),
        "bitrate_kbps": int(settings.mobile_audio_bitrate_kbps),
        "chapters": entries,
    }


__all__ = [
    "FFmpegError",
    "catalog",
    "chapter_timeline",
    "ensure_mobile_audio",
    "mobile_audio_path",
    "mobile_dir",
]
