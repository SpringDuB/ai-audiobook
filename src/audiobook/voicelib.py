"""音色库的写操作：上传新音色、停用/启用。

目录约定和内置音色完全一致：``data/voices/<id>/{voice.json, ref.wav}``。
这样 TTS 合成（engines/http_tts.py 把 ref.wav 传给推理服务）和选角
（analysis/casting.py 读 voice.json）都不需要为"上传音色"分叉。

停用 = voice.json 里写 ``disabled: true``：
- /api/voices 默认不列它（音色库页面单独放"已停用"区）；
- 选角提示词不喂给大模型；
- 选音色悬浮窗看不到；
- ref.wav / voice.json 都还在，随时能再启用。
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from . import store
from .audio import wav_duration
from .render.ffmpeg import FFmpegError, probe_wav, run_ffmpeg

VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".webm")
MAX_AUDIO_BYTES = 30 * 1024 * 1024
MIN_REF_SECONDS = 1.0
MAX_REF_SECONDS = 60.0


def safe_voice_id(voice_id: str) -> str:
    value = str(voice_id or "").strip()
    if not VOICE_ID_PATTERN.match(value):
        raise ValueError("非法的音色 id")
    return value


def read_voice_meta(settings, voice_id: str) -> dict:
    """读某个音色的 voice.json；不存在返回 {}。"""
    path = store.voice_path(settings, safe_voice_id(voice_id))
    data = store.read_json(path, default={}) or {}
    return data if isinstance(data, dict) else {}


def next_voice_id(voices_dir: Path) -> str:
    used = {path.name for path in voices_dir.iterdir() if path.is_dir()} if voices_dir.exists() else set()
    for index in range(1, 10000):
        candidate = f"u{index:03d}"
        if candidate not in used:
            return candidate
    raise RuntimeError("音色 id 用光了（u0001–u9999）")


def _split_list(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[/,，、;；\s]+", values)
    out: list[str] = []
    for value in values or ():
        piece = str(value).strip()
        if piece and piece not in out:
            out.append(piece)
    return out


def _to_ref_wav(settings, source: Path, target: Path) -> None:
    """统一转成单声道 16bit PCM WAV —— TTS 服务的参考音频只认 wav。"""
    try:
        info = probe_wav(source)
    except Exception:  # noqa: BLE001 - 不是 wav 就走 ffmpeg
        info = None
    if info is not None and info.channels == 1 and info.bits == 16:
        shutil.copyfile(source, target)  # 已经是能直接用的 wav，不折腾 ffmpeg
        return
    try:
        run_ffmpeg(
            settings,
            ["-i", str(source), "-vn", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
            timeout=300,
        )
    except FFmpegError as exc:
        raise ValueError(f"这个音频解码不了，换一个文件试试：{exc}") from exc


def add_voice(
    settings,
    *,
    name: str,
    audio: bytes,
    filename: str,
    gender: str = "",
    age_group: str = "",
    speech_rate: str = "",
    usage_type=(),
    tags=(),
    description: str = "",
) -> dict:
    """把上传的参考音频收进音色库，返回新音色的 voice.json 内容。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("音色名称不能为空")
    if not audio:
        raise ValueError("参考音频是空的")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError(f"参考音频太大（{len(audio) // 1024 // 1024}MB > {MAX_AUDIO_BYTES // 1024 // 1024}MB）")
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError("参考音频支持 wav / mp3 / m4a / flac / ogg 等常见格式")

    voices_dir = Path(settings.voices_dir)
    voices_dir.mkdir(parents=True, exist_ok=True)
    voice_id = next_voice_id(voices_dir)
    target_dir = voices_dir / voice_id
    target_dir.mkdir(parents=True)
    upload = target_dir / f".upload{suffix}"
    ref = target_dir / "ref.wav"
    try:
        store.atomic_write_bytes(upload, audio)
        _to_ref_wav(settings, upload, ref)
        info = probe_wav(ref)
        if info.duration < MIN_REF_SECONDS:
            raise ValueError(f"参考音频太短（{info.duration:.1f}s）：建议 5–15 秒干净人声")
        if info.duration > MAX_REF_SECONDS:
            raise ValueError(f"参考音频太长（{info.duration:.0f}s）：截到 {MAX_REF_SECONDS:.0f} 秒以内更稳")
        meta = {
            "id": voice_id,
            "name": name,
            "gender": str(gender or "").strip(),
            "age_group": str(age_group or "").strip(),
            "speech_rate": str(speech_rate or "").strip(),
            "usage_type": _split_list(usage_type),
            "tags": _split_list(tags),
            "description": str(description or "").strip(),
            "source": "upload",
            "created_at": int(time.time() * 1000),
            "disabled": False,
            "ref": {
                "file": "ref.wav",
                "bytes": ref.stat().st_size,
                "sample_rate": info.sample_rate,
                "channels": info.channels,
                "duration": round(wav_duration(ref), 3),
            },
        }
        store.atomic_replace_json(target_dir / "voice.json", meta)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)  # 失败不留半个音色
        raise
    finally:
        upload.unlink(missing_ok=True)
    return meta


def set_disabled(settings, voice_id: str, disabled: bool) -> dict:
    """停用/启用一个音色（只动 disabled 字段，音频与元数据都留着）。"""
    voice_id = safe_voice_id(voice_id)
    path = store.voice_path(settings, voice_id)
    meta = store.read_json(path, default=None)
    if not isinstance(meta, dict):
        raise FileNotFoundError(f"音色不存在：{voice_id}")
    meta = {**meta, "disabled": bool(disabled)}
    store.atomic_replace_json(path, meta)
    return meta
