import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import audio, jobs, listen, store, voicelib
from ..analysis.design import save_description
from ..analysis.voices import description_key, preview_path, write_preview_meta
from ..engines.factory import build_engine
from ..analysis.derive import derive_lang as _derive_lang
from ..config import EMOTION_TEXT_ENABLED, OVERLAY_KEYS, get_settings, load_overlay, save_overlay
from ..editing import apply_line_patch, invalidate_chapter
from ..importer import import_book
from ..pipeline import resume_book
from ..tts_service import LocalTtsService

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _reveal_directory(path: Path) -> bool:
    """在系统文件管理器里打开目录。失败只返回 False，交给界面提示。"""
    try:
        target = Path(path).resolve()
        if sys.platform.startswith("win"):
            opener = getattr(os, "startfile", None)
            if opener is None:
                return False
            opener(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception:
        return False


def _output_info(settings, book_id: str) -> dict:
    """导出产物清单：界面用它决定「打开成果文件夹」能不能点。"""
    directory = store.output_dir(settings, book_id)
    files: list[dict] = []
    if directory.exists():
        for path in sorted(directory.iterdir()):
            if path.is_file():
                files.append({"name": path.name, "size": path.stat().st_size})
    return {"dir": str(directory), "exists": directory.exists(), "files": files}


def _character_names(settings, book_id: str) -> dict[str, str]:
    payload = store.read_json(store.characters_path(settings, book_id), default={}) or {}
    names = {"narrator": "旁白"}
    for character in payload.get("characters") or []:
        names[character["id"]] = character["name"]
    return names


def _chapter_meta(settings, book_id: str, index: int) -> dict | None:
    for chapter in store.chapter_list(settings, book_id):
        if int(chapter["index"]) == index:
            return chapter
    return None


_OCCURRENCES_CACHE_LOCK = threading.Lock()
_OCCURRENCES_CACHE: dict[str, tuple[tuple, dict[str, dict]]] = {}


def _copy_occurrences(payload: dict[str, dict]) -> dict[str, dict]:
    """给调用方一份独立副本：缓存里的 chapters 列表不能被外部改掉。"""
    return {
        role_id: {"chapters": list(bucket.get("chapters") or []), "lines": int(bucket.get("lines") or 0)}
        for role_id, bucket in payload.items()
    }


def _role_occurrences(settings, book_id: str) -> dict[str, dict]:
    """扫一遍逐句标注，统计每个角色出现在哪些章节、共多少句。

    这本书所有 lines.jsonl 都要读一遍（256 章 2.3 万行），而选角页刷一次就得算一次，
    所以按"每个 lines 文件的 mtime/size"缓存：文件没动直接复用，动了立刻重算。
    """
    paths = sorted(store.book_dir(settings, book_id).glob("analysis/lines/chapter_*.jsonl"))
    signature = tuple((path.name, store.file_signature(path)) for path in paths)
    cache_key = f"{settings.data_dir}|{book_id}"
    with _OCCURRENCES_CACHE_LOCK:
        cached = _OCCURRENCES_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return _copy_occurrences(cached[1])

    result: dict[str, dict] = {}
    for path in paths:
        index = int(path.stem.rsplit("_", 1)[-1])
        rows = store.read_jsonl(path)
        for row in rows:
            for key in ("speaker", "addressee"):
                role_id = row.get(key)
                if not role_id:
                    continue
                bucket = result.setdefault(role_id, {"chapters": set(), "lines": 0})
                bucket["chapters"].add(index)
                if key == "speaker":
                    bucket["lines"] += 1
    computed = {
        role_id: {"chapters": sorted(bucket["chapters"]), "lines": bucket["lines"]}
        for role_id, bucket in result.items()
    }
    with _OCCURRENCES_CACHE_LOCK:
        _OCCURRENCES_CACHE[cache_key] = (signature, computed)
    return _copy_occurrences(computed)


def _chapters_with_role(settings, book_id: str, role_id: str) -> list[int]:
    return _role_occurrences(settings, book_id).get(role_id, {}).get("chapters", [])


def _casting_covers_chapter(settings, book_id: str, index: int) -> bool:
    """这一章每个说话人都已经有发声方式了吗？（基础音色描述 / 手工绑的库存音色）"""
    from ..analysis.voices import resolve_line_voice

    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    if not (casting.get("roles") or {}):
        return False
    for row in store.read_jsonl(store.lines_path(settings, book_id, index)):
        if not row.get("speaker"):
            continue
        plan = resolve_line_voice(settings, book_id, row, casting)
        if plan.source == "design-missing":
            return False
        if plan.source == "library" and not store.voice_ref_path(settings, plan.voice_id).exists():
            return False
    return True


def _preview_state(settings, book_id: str, role_id: str, description: str) -> dict:
    """角色试听：文件在不在（exists）、是不是按当前描述生成的（current）。"""
    path = preview_path(settings, book_id, role_id)
    if not path.exists():
        return {"exists": False, "current": False}
    meta = store.read_json(path.with_suffix(".json"), default={}) or {}
    return {
        "exists": True,
        "current": meta.get("description_key") == description_key(description),
    }


def _role_payload(settings, book_id: str, role_id: str, role: dict, names: dict, occurrences: dict) -> dict:
    """角色行给前端的形状：登记信息 + 出场统计 + 试听状态。"""
    preview = _preview_state(settings, book_id, role_id, role.get("description") or "")
    return {
        **role,
        "name": role.get("name") or names.get(role_id, role_id),
        "chapters": occurrences.get(role_id, {}).get("chapters", []),
        "lines": occurrences.get(role_id, {}).get("lines", 0),
        "has_preview": preview["exists"],
        "preview_current": preview["current"],
    }


def _lines_payload(settings, book_id: str, index: int) -> list[dict]:
    rows = store.read_jsonl(store.lines_path(settings, book_id, index))
    clips_dir = store.audio_dir(settings, book_id, index)
    payload = []
    for position, row in enumerate(rows):
        # 老数据没有 seq：按行序兜底，别让校对台显示 null
        clip = clips_dir / f"{row['id']}.wav"
        duration = 0.0
        # 音频版本号（mtime）：单句重生成后 URL 不变，靠它让浏览器别拿旧缓存
        audio_mtime = None
        if clip.exists():
            try:
                duration = round(audio.wav_duration(clip), 3)
                audio_mtime = round(clip.stat().st_mtime, 3)
            except Exception:  # noqa: BLE001 - 半截文件不该让接口 500
                duration = 0.0
        payload.append(
            {
                **{
                    key: row.get(key)
                    for key in (
                        "id", "scene", "speaker", "speaker_name",
                        "addressee", "addressee_name", "text", "voice_prompt",
                        "emotion", "delivery", "rate", "lang",
                    )
                },
                "duration_sec": duration,
                "has_audio": clip.exists(),
                "audio_mtime": audio_mtime,
                "audio_url": f"/api/books/{book_id}/lines/{row['id']}/audio",
                "seq": row.get("seq") or position + 1,
                "scene_index": row.get("scene_index") or 1,
            }
        )
    return payload


# SSE 任务流：只推"活跃 + 最近 N 条"的窗口。
# 旧实现每秒重发全量（2400+ 条 = 675KB，实测 755KB/s ≈ 2.7GB/小时），
# 根因是快照里带 ts（每秒都变）导致"变了才发"永远成立，且快照是全库历史。
SSE_ACTIVE_STATUSES = ("running", "queued")
SSE_RECENT_JOBS = 30
SSE_POLL_SECONDS = 1.0
SSE_PING_SECONDS = 25.0


def jobs_snapshot(conn, book_id: str | None = None) -> dict:
    """SSE 推送的任务窗口：所有活跃任务 + 最近 N 条（含刚失败的）。

    任务中心要看完整历史时走 GET /api/jobs 的 status/limit 参数，
    别让事件流带上全库（2200+ 条 = 675KB/帧）。
    """
    active = jobs.list_jobs(conn, book_id, statuses=list(SSE_ACTIVE_STATUSES))
    seen = {job.id for job in active}
    recent = jobs.list_jobs(conn, book_id, limit=SSE_RECENT_JOBS)
    merged = active + [job for job in recent if job.id not in seen]
    merged.sort(key=lambda job: job.id)
    return {"jobs": [job.__dict__ for job in merged]}


def sse_frame(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class JobStream:
    """把窗口快照做成增量帧：首帧全量，之后只发真正变了的条目。

    delta=True （新前端，/api/events?v=2）：snapshot + patch(changed/removed)。
    delta=False（老前端只认 jobs 字段）：仍然发窗口全量，但只在有变化时发，
                且不再带 ts —— 避免旧缓存页面在升级后拿不到数据。
    """

    def __init__(self, conn, book_id: str | None = None, *, delta: bool = True) -> None:
        self.conn = conn
        self.book_id = book_id
        self.delta = bool(delta)
        self._jobs: dict[int, dict] = {}
        self._first = True

    def poll(self) -> str | None:
        """返回一帧 SSE 文本；没有变化时返回 None（不发任何字节）。"""
        current = {item["id"]: item for item in jobs_snapshot(self.conn, self.book_id)["jobs"]}
        if self._first:
            self._first = False
            self._jobs = current
            if self.delta:
                return sse_frame({"type": "snapshot", "jobs": list(current.values())})
            return sse_frame({"jobs": list(current.values())})
        changed = [item for key, item in current.items() if self._jobs.get(key) != item]
        removed = [key for key in self._jobs if key not in current]
        self._jobs = current
        if not changed and not removed:
            return None
        if self.delta:
            return sse_frame({"type": "patch", "changed": changed, "removed": removed})
        return sse_frame({"jobs": list(current.values())})


def create_app(settings, conn) -> FastAPI:
    app = FastAPI(title="AI 有声书")
    tts_service = LocalTtsService(settings)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html", media_type="text/html")

        # PWA：service worker 必须从根路径下发（作用域才是整个站点），
        # manifest 同理，浏览器只认同源根下的名字。
        @app.get("/sw.js")
        def service_worker():
            return FileResponse(
                WEB_DIR / "sw.js",
                media_type="text/javascript",
                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
            )

        @app.get("/manifest.webmanifest")
        def web_manifest():
            return FileResponse(
                WEB_DIR / "manifest.webmanifest",
                media_type="application/manifest+json",
                headers={"Cache-Control": "no-cache"},
            )

    @app.post("/api/books")
    async def upload_book(file: UploadFile = File(...), title: str = Form("未命名")):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".txt", ".epub"):
            raise HTTPException(status_code=400, detail="只支持 txt / epub：换一个文件再传")
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="文件是空的")
        tmp = settings.data_dir / "uploads" / f"upload-{uuid.uuid4().hex[:8]}{suffix}"
        store.atomic_write_bytes(tmp, data)
        book_title = (title or "").strip()
        if not book_title or book_title == "未命名":
            book_title = Path(file.filename or "未命名").stem or "未命名"
        try:
            book_id = import_book(settings, conn, tmp, title=book_title)
        except ValueError as exc:  # epub 坏了 / 空文件等：回 400 让前端提示
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"book_id": book_id, "title": book_title}

    @app.get("/api/books")
    def list_books():
        rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
        return {"books": [{**dict(r), "stats": store.book_stats(settings, r["id"], conn)} for r in rows]}

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: str):
        if not store.valid_book_id(book_id):
            raise HTTPException(status_code=400, detail="非法的书籍 id")
        row = conn.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None and not store.book_dir(settings, book_id).exists():
            raise HTTPException(status_code=404, detail="book not found")
        title = (row["title"] if row is not None else None) or book_id
        busy = store.running_job_count(conn, book_id)
        if busy:
            raise HTTPException(
                status_code=409,
                detail=f"《{title}》还有 {busy} 个任务正在运行：请到任务中心取消，或等它跑完再删除",
            )
        try:
            result = store.delete_book(settings, conn, book_id)
        except ValueError as exc:  # 目录越界等情况：宁可报错也别乱删
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": True, **result}

    @app.get("/api/books/{book_id}/chapters")
    def book_chapters(book_id: str):
        chapters = store.chapter_index(settings, book_id)
        if not chapters:
            meta = store.read_json(store.book_dir(settings, book_id) / "book.json", default={}) or {}
            if not meta:
                raise HTTPException(status_code=404, detail="book not found")
            # 刚导入：分章还在队列里。这里返回 200 + 空列表，前端显示"正在分章"并自动刷新；
            # 返回 404 会让刚导入就跳进来的书页变成一个假错误。
            return {"chapters": [], "status": meta.get("status") or "imported", "pending": True}
        payload = []
        for chapter in chapters:
            index = int(chapter["index"])
            payload.append(
                {
                    "index": index,
                    "title": chapter.get("title") or f"第{index}章",
                    "chars": chapter.get("chars"),
                    **store.chapter_state(settings, book_id, index),
                }
            )
        return {"chapters": payload, "status": "split", "pending": False}

    @app.get("/api/books/{book_id}/chapters/{index}/lines")
    def chapter_lines(book_id: str, index: int):
        if _chapter_meta(settings, book_id, index) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        return {"lines": _lines_payload(settings, book_id, index)}

    @app.get("/api/books/{book_id}/chapters/{index}/text")
    def chapter_text(book_id: str, index: int):
        chapter = _chapter_meta(settings, book_id, index)
        if chapter is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        return {
            "index": index,
            "title": chapter.get("title") or f"第{index}章",
            "chars": chapter.get("chars") or len(chapter.get("content") or ""),
            "content": chapter.get("content") or "",
        }

    @app.get("/api/books/{book_id}/lines/{line_id}/audio")
    def line_audio(book_id: str, line_id: str):
        for chapter_dir in sorted(store.book_dir(settings, book_id).glob("audio/chapter_*")):
            clip = chapter_dir / f"{line_id}.wav"
            if clip.exists():
                # 同一句重生成后路径不变；不让浏览器拿旧缓存，否则试听听到的还是上一版
                return FileResponse(clip, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404, detail="audio not ready")

    @app.get("/api/books/{book_id}/issues")
    def book_issues(book_id: str):
        rows = store.read_jsonl(store.issues_path(settings, book_id))
        return {"issues": list(reversed(rows))[:200]}

    def _voice_payload(path, meta: dict) -> dict:
        voice_id = meta.get("id") or path.parent.name
        # 迁移过来的音色把标签拆成了好几栏，这里合并一份方便前端分类
        tags = []
        for key in ("tags", "personality", "genres", "mood", "voice_quality", "language_style"):
            for value in meta.get(key) or []:
                if value and value not in tags:
                    tags.append(value)
        ref = path.parent / "ref.wav"
        return {
            "id": voice_id,
            "name": meta.get("name") or voice_id,
            "gender": meta.get("gender"),
            "age_group": meta.get("age_group"),
            "speech_rate": meta.get("speech_rate"),
            "personality": meta.get("personality") or [],
            "genres": meta.get("genres") or [],
            "mood": meta.get("mood") or [],
            "voice_quality": meta.get("voice_quality") or [],
            "usage_type": meta.get("usage_type") or [],
            "description": meta.get("description") or "",
            "tags": tags,
            "source": meta.get("source") or "builtin",
            "disabled": bool(meta.get("disabled")),
            "created_at": meta.get("created_at"),
            "needs_review": bool(meta.get("needs_review")),
            "has_ref": ref.exists(),
            "sample_url": f"/api/voices/{voice_id}/sample",
        }

    @app.get("/api/voices")
    def list_voices(include_disabled: bool = False):
        """音色库默认只给"启用的"：停用的音色对大模型和选音色都不可见。"""
        voices = []
        for path in sorted(settings.voices_dir.glob("*/voice.json")):
            meta = store.read_json(path, default={}) or {}
            payload = _voice_payload(path, meta)
            if payload["disabled"] and not include_disabled:
                continue
            voices.append(payload)
        return {"voices": voices}

    @app.post("/api/voices")
    async def upload_voice(
        file: UploadFile = File(...),
        name: str = Form(...),
        gender: str = Form(""),
        age_group: str = Form(""),
        speech_rate: str = Form(""),
        usage_type: str = Form(""),
        tags: str = Form(""),
        description: str = Form(""),
    ):
        """上传一个音色：参考音频 + 标签介绍 → data/voices/<id>/。"""
        data = await file.read()
        try:
            meta = voicelib.add_voice(
                settings,
                name=name,
                audio=data,
                filename=file.filename or "",
                gender=gender,
                age_group=age_group,
                speech_rate=speech_rate,
                usage_type=usage_type,
                tags=tags,
                description=description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"voice": _voice_payload(settings.voices_dir / meta["id"] / "voice.json", meta)}

    @app.patch("/api/voices/{voice_id}")
    def patch_voice(voice_id: str, payload: dict):
        """停用 / 启用一个音色（音频留着，随时能恢复）。"""
        try:
            if "disabled" in payload:
                meta = voicelib.set_disabled(settings, voice_id, bool(payload["disabled"]))
            else:
                meta = voicelib.read_voice_meta(settings, voice_id)
                if not meta:
                    raise FileNotFoundError(f"音色不存在：{voice_id}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = settings.voices_dir / (meta.get("id") or voice_id) / "voice.json"
        return {"voice": _voice_payload(path, meta)}

    @app.get("/api/voices/{voice_id}/sample")
    def voice_sample(voice_id: str):
        base = settings.voices_dir / voice_id
        for name in ("sample.wav", "ref.wav"):
            path = base / name
            if path.exists():
                return FileResponse(path, media_type="audio/wav")
        raise HTTPException(status_code=404, detail="sample not found")

    @app.get("/api/books/{book_id}/casting")
    def book_casting(book_id: str):
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        names = _character_names(settings, book_id)
        occurrences = _role_occurrences(settings, book_id)
        roles = [
            _role_payload(settings, book_id, role_id, role, names, occurrences)
            for role_id, role in (casting.get("roles") or {}).items()
        ]
        roles.sort(key=lambda role: (-role["lines"], role["role_id"]))
        voices = []
        for path in sorted(settings.voices_dir.glob("*/voice.json")):
            meta = store.read_json(path, default={}) or {}
            if meta.get("disabled"):
                continue  # 停用的音色不出现在任何选择入口
            voices.append({"id": path.parent.name, "name": meta.get("name") or path.parent.name})
        role_ids = [role["role_id"] for role in roles]
        return {
            "narrator_voice": casting.get("narrator_voice") or "default",
            "roles": roles,
            "role_ids": role_ids,
            "voices": voices,
        }

    @app.get("/api/settings")
    def read_settings():
        fresh = _fresh_settings()
        payload = {key: getattr(settings, key) for key in OVERLAY_KEYS}
        payload.update({key: getattr(fresh, key) for key in OVERLAY_KEYS})
        payload["llm_api_key_set"] = bool(fresh.llm_api_key)
        return {
            "settings": payload,
            "overlay_keys": sorted(load_overlay(fresh)),
            # 文本描述情绪通道是否开放（暂时关闭，实现保留）
            "emotion_text_enabled": EMOTION_TEXT_ENABLED,
        }

    def _fresh_settings():
        """运行中服务实际生效的设置 = 启动时的设置 + data/settings.json 覆盖层。"""
        return settings.model_copy(update=load_overlay(settings))

    def _find_line(book_id: str, line_id: str) -> tuple[int, list[dict], int]:
        for path in sorted(store.book_dir(settings, book_id).glob("analysis/lines/chapter_*.jsonl")):
            rows = store.read_jsonl(path)
            for position, row in enumerate(rows):
                if row["id"] == line_id:
                    return int(path.stem.rsplit("_", 1)[-1]), rows, position
        raise HTTPException(status_code=404, detail="line not found")

    @app.patch("/api/books/{book_id}/lines/{line_id}")
    def patch_line(book_id: str, line_id: str, patch: dict):
        index, rows, position = _find_line(book_id, line_id)
        try:
            rows[position] = apply_line_patch(rows[position], patch, _character_names(settings, book_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.write_jsonl_atomic(store.lines_path(settings, book_id, index), rows)
        removed = invalidate_chapter(settings, book_id, index)
        return {"line": rows[position], "invalidated": {"chapter": index, "render_meta_removed": removed}}

    @app.post("/api/books/{book_id}/lines/{line_id}/resynth")
    def resynth_line(book_id: str, line_id: str):
        index, _rows, _position = _find_line(book_id, line_id)
        invalidate_chapter(settings, book_id, index)
        return {"job_id": jobs.enqueue_line(conn, book_id, index, line_id), "chapter_index": index}

    @app.post("/api/books/{book_id}/export")
    def export_book_route(book_id: str, payload: dict | None = None):
        payload = payload or {}
        return {"job_id": jobs.enqueue(conn, "book_export", book_id), "mode": payload.get("mode") or "all"}

    @app.get("/api/books/{book_id}/output")
    def book_output(book_id: str):
        """导出目录清单：有没有产物、里面有哪些文件。"""
        if not store.book_dir(settings, book_id).exists():
            raise HTTPException(status_code=404, detail="book not found")
        return _output_info(settings, book_id)

    @app.post("/api/books/{book_id}/output/reveal")
    def reveal_book_output(book_id: str):
        """在系统文件管理器里打开导出目录（本机单用户工具，服务端直接打开）。"""
        if not store.book_dir(settings, book_id).exists():
            raise HTTPException(status_code=404, detail="book not found")
        directory = store.output_dir(settings, book_id)
        if not directory.exists():
            raise HTTPException(status_code=404, detail="还没有导出产物：先跑「导出整本成品」")
        return {"opened": _reveal_directory(directory), "dir": str(directory.resolve())}

    @app.post("/api/books/{book_id}/chapters/{index}/render")
    def render_chapter_route(book_id: str, index: int):
        """强制重渲染一章：作废该章成品与整本成品，然后入队 post。"""
        if _chapter_meta(settings, book_id, index) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        invalidate_chapter(settings, book_id, index)
        return {"job_id": jobs.enqueue(conn, "post", book_id, index), "chapter_index": index}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: int):
        if jobs.get_job(conn, job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"ok": jobs.retry(conn, job_id)}

    @app.post("/api/books/{book_id}/issues/retry")
    def retry_issues(book_id: str, payload: dict | None = None):
        kinds = set((payload or {}).get("kinds") or [])
        chapters = sorted(
            {
                int(row["chapter"])
                for row in store.read_jsonl(store.issues_path(settings, book_id))
                if row.get("chapter") is not None and (not kinds or row.get("kind") in kinds)
            }
        )
        job_ids = [jobs.enqueue(conn, "synthesize", book_id, index) for index in chapters]
        return {"chapters": chapters, "job_ids": job_ids}

    @app.put("/api/books/{book_id}/casting/{role_id}")
    def update_casting(book_id: str, role_id: str, payload: dict):
        """手工给角色绑一个库存音色（备用通道）：voice_source 变 library，合成走克隆。"""
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        roles = casting.setdefault("roles", {})
        if role_id not in roles:
            known = _character_names(settings, book_id)
            if role_id not in known:
                raise HTTPException(status_code=404, detail="role not found")
            # 角色分析里有、选角落下的角色：允许在这里手工补一条
            roles[role_id] = {
                "role_id": role_id,
                "name": known[role_id],
                "voice_id": "default",
                "voice_name": "",
                "score": None,
                "reasons": [],
                "overrides": {},
            }
            casting["roles"] = roles
            store.atomic_replace_json(store.casting_path(settings, book_id), casting)
        roles[role_id]["voice_id"] = str(payload.get("voice_id") or roles[role_id].get("voice_id") or "default")
        roles[role_id]["overrides"] = payload.get("overrides") or roles[role_id].get("overrides") or {}
        roles[role_id]["source"] = "manual"
        # 手工绑库存音色：这条记录改成走克隆通道（合成时会用音色库里的 ref.wav）
        roles[role_id]["voice_source"] = "library"
        voice_name = payload.get("voice_name")
        if voice_name:
            roles[role_id]["voice_name"] = str(voice_name)
        store.atomic_replace_json(store.casting_path(settings, book_id), casting)
        chapters = _chapters_with_role(settings, book_id, role_id)
        for index in chapters:
            invalidate_chapter(settings, book_id, index)
        return {"role": roles[role_id], "invalidated": chapters}

    @app.put("/api/books/{book_id}/roles/{role_id}/description")
    def update_role_description(book_id: str, role_id: str, payload: dict):
        """保存角色基础音色描述（用户手改）：描述一改，这个角色的旧音频自动按新描述重生成。"""
        if store.read_json(store.casting_path(settings, book_id), default={}) is None:
            raise HTTPException(status_code=404, detail="book not found")
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        role = (casting.get("roles") or {}).get(role_id)
        if not role:
            raise HTTPException(status_code=404, detail="role not found")
        description = str(payload.get("description") or "").strip()
        if not description:
            raise HTTPException(status_code=400, detail="音色描述不能为空")
        entry = save_description(
            settings,
            book_id,
            role_id,
            description=description,
            sample=str(payload.get("sample") or role.get("sample") or "").strip(),
            source="manual",
            name=role.get("name"),
        )
        return {"role": entry}

    @app.post("/api/books/{book_id}/roles/{role_id}/rewrite")
    def rewrite_role_description(book_id: str, role_id: str, payload: dict | None = None):
        """让大模型重新写一版音色描述（异步任务，跑完前端刷新就能看到）。

        mode=refine（默认）：上一版当锚点微调，音色基本不变，只修缺维度/自相矛盾；
        mode=reroll：重掷一版音色；带 instruction 时按用户写的要求设计
        （"换成四十岁左右的低沉男声"这种，硬性生效）。
        """
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        if role_id not in (casting.get("roles") or {}):
            raise HTTPException(status_code=404, detail="role not found")
        payload = payload or {}
        mode = str(payload.get("mode") or "refine").strip().lower()
        if mode not in ("refine", "reroll"):
            raise HTTPException(status_code=400, detail="mode 只能是 refine 或 reroll")
        instruction = str(payload.get("instruction") or "").strip()[:300]
        wanted = {"roles": [role_id], "force": True, "mode": mode}
        if instruction:
            wanted["instruction"] = instruction
        job_id = jobs.enqueue(
            conn, "voice_design", book_id, payload=wanted
        )
        # enqueue 对同 (kind, book, chapter) 幂等：上一次还在排队时会把 payload 换成
        # 这一次的（例如上次点"微调"、这次点"换一版音色"），否则用户的新意图会被丢掉。
        job = jobs.get_job(conn, job_id)
        if job is not None and job.status == "queued" and (job.payload or {}) != wanted:
            jobs.set_payload(conn, job_id, wanted)
        return {"job_id": job_id}

    @app.post("/api/books/{book_id}/roles/{role_id}/preview")
    def generate_role_preview(book_id: str, role_id: str, payload: dict | None = None):
        """按当前描述生成角色试听（Qwen3-TTS VoiceDesign + 该角色的试音台词）。

        第一次点要生成几秒音频（模型没加载时要先加载），之后再点直接听缓存文件。
        """
        payload = payload or {}
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        role = (casting.get("roles") or {}).get(role_id)
        if not role:
            raise HTTPException(status_code=404, detail="role not found")
        description = str(payload.get("description") or role.get("description") or "").strip()
        if not description:
            raise HTTPException(status_code=400, detail="这个角色还没有音色描述：先点「重写描述」或自己写一段")
        sample = str(payload.get("sample") or role.get("sample") or "").strip() or "你先坐下，慢慢说，我听着呢。"
        if payload.get("description") is not None or payload.get("sample") is not None:
            role = save_description(
                settings,
                book_id,
                role_id,
                description=description,
                sample=sample,
                source="manual",
                name=role.get("name"),
            )
        out_path = preview_path(settings, book_id, role_id)
        engine = None
        try:
            engine = build_engine(settings)
            result = engine.design_voice(
                instruct=description,
                text=sample,
                lang=_derive_lang(sample),
                out_path=out_path,
            )
        except Exception as exc:  # noqa: BLE001 - 试听失败要把原因原样给界面
            raise HTTPException(status_code=503, detail=f"试听生成失败：{type(exc).__name__}: {exc}") from exc
        finally:
            close = getattr(engine, "close", None)
            if callable(close):
                close()
        write_preview_meta(
            settings,
            book_id,
            role_id,
            {
                "role_id": role_id,
                "description_key": description_key(description),
                "sample": sample,
                "duration": round(float(result.duration), 3),
                "updated_at": int(time.time() * 1000),
            },
        )
        return {
            "ok": True,
            "duration": round(float(result.duration), 3),
            "sample": sample,
            "updated_at": int(time.time() * 1000),
        }

    @app.post("/api/books/{book_id}/roles/preview_all")
    def generate_all_role_previews(book_id: str, payload: dict | None = None):
        """一键生成全部角色试听（异步任务）：已生成且描述没变的自动跳过。"""
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        if not (casting.get("roles") or {}):
            raise HTTPException(status_code=404, detail="role not found")
        payload = payload or {}
        wanted = {"force": bool(payload.get("force"))}
        roles = [str(item) for item in payload.get("roles") or [] if item]
        if roles:
            wanted["roles"] = roles
        job_id = jobs.enqueue(conn, "voice_preview", book_id, payload=wanted)
        job = jobs.get_job(conn, job_id)
        if job is not None and job.status == "queued" and (job.payload or {}) != wanted:
            jobs.set_payload(conn, job_id, wanted)
        return {"job_id": job_id}

    @app.get("/api/books/{book_id}/roles/{role_id}/preview.wav")
    def role_preview_audio(book_id: str, role_id: str):
        path = preview_path(settings, book_id, role_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="还没有试听音频：先点「试听」生成")
        return FileResponse(path, media_type="audio/wav")

    @app.put("/api/settings")
    def update_settings(payload: dict):
        try:
            overlay = save_overlay(settings, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fresh = _fresh_settings()
        return {
            "settings": {
                **{key: getattr(fresh, key) for key in OVERLAY_KEYS},
                "llm_api_key_set": bool(fresh.llm_api_key),
            },
            "overlay_keys": sorted(overlay),
            "emotion_text_enabled": EMOTION_TEXT_ENABLED,
        }

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str, chapters: str = "full"):
        """书目信息。默认带上分章结果（含正文，实测一本 2.8MB）；
        界面打开书籍只需要书名和产出清单，用 ``?chapters=none`` 别把整本书拖下来。"""
        meta = store.read_json(store.book_dir(settings, book_id) / "book.json")
        if meta is None:
            raise HTTPException(status_code=404, detail="book not found")
        payload = {} if chapters == "none" else store.read_chapters(settings, book_id)
        return {"book": meta, "chapters": payload, "output": _output_info(settings, book_id)}

    @app.post("/api/books/{book_id}/run")
    def run_book(book_id: str):
        plan = resume_book(settings, conn, book_id)
        return {"ok": True, "queued": len(plan), "plan": plan}

    @app.post("/api/books/{book_id}/analyze")
    def analyze_book(book_id: str, force: bool = False):
        """只推分析链：分章 → 整章分析（角色 + 逐句情感）→ 选角。

        force=true 时无视断点整本重跑，会覆盖已分析的逐句标注（含人工修改）；
        默认（force=false）已经分析好的章节会被跳过。
        """
        plan = resume_book(settings, conn, book_id, phase="analysis", force=force)
        if force:
            active = jobs.find_active(conn, "characters", book_id)
            if active is not None:
                # enqueue 是幂等的：force 得写进 payload，handler 才知道要不要重提
                jobs.set_payload(conn, active.id, {"force": True})
        return {"ok": True, "queued": len(plan), "plan": plan}

    @app.post("/api/books/{book_id}/chapters/{index}/analyze")
    def analyze_chapter(book_id: str, index: int):
        """只重跑本章的整章分析：提取 + 落行，不碰其它章，也不要求先有角色表。

        没有 characters.json 时由 lines handler 增量补角色（新称呼并进角色表，单章可独立跑完）。
        """
        if _chapter_meta(settings, book_id, index) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        # 删掉本章的提取结果 → lines handler 会重新调 LLM 提取（而不是只重算落盘）
        store.extract_path(settings, book_id, index).unlink(missing_ok=True)
        plan: list[tuple[str, int | None]] = [("lines", index)]
        job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
        return {"ok": True, "queued": len(job_ids), "plan": plan, "job_ids": job_ids}

    @app.post("/api/books/{book_id}/analyze/chapters")
    def analyze_chapters(book_id: str, payload: dict | None = None):
        """只重跑勾选章节的分析：默认跳过已经分析好的章，force=true 才覆盖重跑。

        force 也不会预先删掉提取结果：批量任务轮到哪一章才覆盖哪一章，
        中途取消的话，没轮到的章还留着旧结果（不会白丢已分析好的内容）。

        勾选全部章节时走整书 characters 任务（跨章合并同人异名更准，跑完会自动
        为每章排队 lines）；勾选一部分时走一个 chapters 批量任务：job 内部按大模型
        并发同时提这几章（和 characters 同一模式），新称呼并进现有角色表。
        """
        body = payload or {}
        try:
            picked = sorted({int(item) for item in (body.get("chapters") or [])})
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="chapters 必须是章节序号数组") from exc
        if not picked:
            raise HTTPException(status_code=400, detail="至少选择一章")
        force = bool(body.get("force"))
        chapters = store.chapter_index(settings, book_id)
        known = {int(chapter["index"]) for chapter in chapters}
        if not known:
            raise HTTPException(status_code=409, detail="还没有分章结果，请先分章")
        unknown = [index for index in picked if index not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"不存在的章节：{unknown}")
        if len(picked) == len(known):
            job_id = jobs.enqueue(conn, "characters", book_id)
            if force:
                jobs.set_payload(conn, job_id, {"force": True})
            return {"ok": True, "queued": 1, "plan": [["characters", None]], "job_ids": [job_id]}

        active = jobs.find_active(conn, "chapters", book_id)
        if active is not None and active.status == "running":
            # 已经在跑的批量任务只认它开始时那份清单：这次勾选里它没覆盖的章退回按章
            # lines（各章一个 job），保证不会静默丢单。它已覆盖的章交给它自己跑。
            covered = {int(item) for item in ((active.payload or {}).get("chapters") or [])}
            extra = [index for index in picked if index not in covered]
            plan = [("lines", index) for index in extra]
            job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
            return {
                "ok": True,
                "queued": len(job_ids),
                "plan": plan,
                "job_ids": job_ids,
                "running_job_id": active.id,
            }

        if active is not None:
            # 还没开跑：把这次勾选并进同一条批量任务，避免两次点击各跑一批
            merged = sorted({int(item) for item in ((active.payload or {}).get("chapters") or [])} | set(picked))
            merged_force = force or bool((active.payload or {}).get("force"))
            merged_payload = {"chapters": merged}
            if merged_force:
                merged_payload["force"] = True
            jobs.set_payload(conn, active.id, merged_payload)
            return {"ok": True, "queued": 1, "plan": [["chapters", None]], "job_ids": [active.id]}

        job_payload = {"chapters": picked}
        if force:
            job_payload["force"] = True
        job_id = jobs.enqueue(conn, "chapters", book_id, payload=job_payload)
        plan = [("chapters", None)]
        return {"ok": True, "queued": 1, "plan": plan, "job_ids": [job_id]}

    @app.post("/api/books/{book_id}/generate")
    def generate_book(book_id: str):
        """只推合成链：逐句合成 → 章节渲染 → 整本合本。"""
        plan = resume_book(settings, conn, book_id, phase="audio")
        return {"ok": True, "queued": len(plan), "plan": plan}

    @app.post("/api/books/{book_id}/chapters/{index}/generate")
    def generate_chapter(book_id: str, index: int):
        """只生成这一章：逐句合成 → 本章渲染（post 由 synthesize 自动入队）。"""
        if _chapter_meta(settings, book_id, index) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        if not store.read_jsonl(store.lines_path(settings, book_id, index)):
            raise HTTPException(status_code=409, detail="本章还没有分析结果，先点「分析本章」")
        plan: list[tuple[str, int | None]] = []
        if not _casting_covers_chapter(settings, book_id, index):
            # 角色还没有基础音色描述：先补选角 + 描述，再合成这一章
            plan.append(("casting", None))
            plan.append(("voice_design", None))
        plan.append(("synthesize", index))
        job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
        return {"ok": True, "queued": len(job_ids), "plan": plan, "job_ids": job_ids}

    @app.get("/api/jobs")
    def list_jobs(book_id: str | None = None, status: str | None = None, limit: int | None = None):
        """任务列表。默认全量（老前端照旧）；带 `status=running,queued&limit=200` 时只取需要的那些。"""
        statuses = [item.strip() for item in (status or "").split(",") if item.strip()] or None
        return {"jobs": [j.__dict__ for j in jobs.list_jobs(conn, book_id, statuses=statuses, limit=limit)]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        """单个任务：前端轮询"生成全部试听"这种小批量任务的进度用。"""
        job = jobs.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job": job.__dict__}

    @app.get("/api/tts/status")
    def tts_status():
        from ..engines.factory import build_engine

        try:
            engine = build_engine(settings)
        except Exception as exc:  # noqa: BLE001 - 状态接口不抛错
            return {"engine": settings.engine, "concurrency": 0, "endpoints": [], "error": str(exc)}
        try:
            if hasattr(engine, "status"):
                return {"engine": settings.engine, "error": None, **engine.status()}
            return {
                "engine": settings.engine,
                "concurrency": settings.synth_concurrency,
                "endpoints": [],
                "error": None,
            }
        finally:
            close = getattr(engine, "close", None)
            if callable(close):
                close()

    @app.get("/api/tts/local")
    def tts_local_status():
        fresh = _fresh_settings()
        return {
            "service": tts_service.status(),
            "engine": fresh.engine,
            "endpoints": list(fresh.tts_endpoints),
            "launch": {
                "backend": fresh.tts_backend,
                "model_source": fresh.tts_model_source,
                "model_dir": fresh.tts_model_dir,
                "hf_endpoint": fresh.tts_hf_endpoint,
                "port": fresh.tts_port,
                "emotion_mode": fresh.emotion_mode,
            },
        }

    @app.post("/api/tts/local/start")
    def tts_local_start(payload: dict | None = None):
        payload = payload or {}
        fresh = _fresh_settings()

        def pick(*keys, default):
            """界面发的是 tts_* 前缀的键，手工调用常用短键；两种都认。"""
            for key in keys:
                value = payload.get(key)
                if value not in (None, ""):
                    return value
            return default

        # fake 后端已删除：旧 settings.json / 老请求里的值一律兜到 indextts
        backend = str(pick("backend", "tts_backend", default=fresh.tts_backend))
        if backend != "indextts":
            backend = "indextts"
        port = int(pick("port", "tts_port", default=fresh.tts_port))
        model_source = str(pick("model_source", "tts_model_source", default=fresh.tts_model_source))
        model_dir = str(pick("model_dir", "tts_model_dir", default=fresh.tts_model_dir))
        hf_endpoint = str(pick("hf_endpoint", "tts_hf_endpoint", default=fresh.tts_hf_endpoint))
        emotion_mode = str(pick("emotion_mode", default=fresh.emotion_mode)).lower()
        # 文本描述通道暂时关闭：前端/旧配置就算发了 text，也按 vector 起服务（不加载 QwenEmotion）
        if emotion_mode not in ("text", "vector") or not EMOTION_TEXT_ENABLED:
            emotion_mode = "vector"
        # 情绪通道决定服务端要不要加载 QwenEmotion（约 1.2GB 显存），所以先落盘再启动
        save_overlay(settings, {"emotion_mode": emotion_mode})
        try:
            service = tts_service.start(
                backend=backend,
                port=port,
                model_source=model_source,
                model_dir=model_dir,
                hf_endpoint=hf_endpoint,
                emotion_mode=emotion_mode,
            )
        except (RuntimeError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 一键启动的副作用：主服务自动把合成引擎切到刚起来的本机服务（worker 每轮会重读设置）
        overlay = save_overlay(
            settings,
            {
                "tts_backend": backend,
                "tts_port": service["port"],
                "tts_model_source": model_source,
                "tts_model_dir": model_dir,
                "tts_hf_endpoint": hf_endpoint,
                "engine": "http",
                "tts_endpoints": [service["url"]],
            },
        )
        return {
            "service": service,
            "engine": "http",
            "endpoints": [service["url"]],
            "overlay_keys": sorted(overlay),
        }

    @app.post("/api/tts/local/stop")
    def tts_local_stop():
        return {"service": tts_service.stop()}

    @app.get("/api/tts/local/logs")
    def tts_local_logs(offset: int = 0, limit: int = 300):
        return tts_service.logs(offset=offset, limit=limit)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        jobs.request_cancel(conn, job_id)
        return {"ok": True}

    @app.get("/api/events")
    async def events(book_id: str | None = None, v: int = 0):
        """任务事件流（SSE）。

        只推"活跃 + 最近 30 条"，而且只在真的有变化时才发帧；
        空闲时每 25 秒只有一条 `: ping` 注释保活（2 字节）。
        v=2 走增量帧（patch），不带 v 的老前端拿窗口全量（升级期间的缓存页面不会瞎）。
        """
        stream = JobStream(conn, book_id, delta=int(v or 0) >= 2)

        async def gen():
            last_ping = time.monotonic()
            while True:
                frame = stream.poll()
                if frame is not None:
                    yield frame
                now = time.monotonic()
                if now - last_ping >= SSE_PING_SECONDS:
                    last_ping = now
                    yield ": ping\n\n"
                await asyncio.sleep(SSE_POLL_SECONDS)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/books/{book_id}/chapters/{index}/audio")
    def chapter_audio(book_id: str, index: int):
        path = store.output_dir(settings, book_id) / f"chapter_{store.chapter_tag(index)}.wav"
        if not path.exists():
            raise HTTPException(status_code=404, detail="audio not ready")
        return FileResponse(path, media_type="audio/wav")

    # ---------------------------------------------------------------- 手机听书
    # 章节 wav → m4a（懒转码 + 按 mtime 失效）；只列有成品（wav + srt）的章节。
    @app.get("/api/books/{book_id}/listen")
    def listen_catalog(book_id: str):
        try:
            return listen.catalog(settings, book_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="book not found") from exc

    @app.get("/api/books/{book_id}/chapters/{index}/subtitles")
    def chapter_subtitles(book_id: str, index: int):
        try:
            return listen.chapter_timeline(settings, book_id, index)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "subtitle not ready") from exc

    @app.post("/api/books/{book_id}/chapters/{index}/mobile")
    def prepare_mobile_audio(book_id: str, index: int, payload: dict | None = None):
        force = bool((payload or {}).get("force"))
        try:
            path = listen.ensure_mobile_audio(settings, book_id, index, force=force)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except listen.FFmpegError as exc:
            raise HTTPException(status_code=503, detail=f"转码失败：{exc}") from exc
        return {
            "ok": True,
            "bytes": path.stat().st_size,
            "mtime": round(path.stat().st_mtime, 3),
            "url": f"/api/books/{book_id}/chapters/{index}/audio.m4a",
        }

    @app.get("/api/books/{book_id}/chapters/{index}/audio.m4a")
    def chapter_audio_mobile(book_id: str, index: int):
        try:
            path = listen.ensure_mobile_audio(settings, book_id, index)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except listen.FFmpegError as exc:
            raise HTTPException(status_code=503, detail=f"转码失败：{exc}") from exc
        return FileResponse(
            path,
            media_type="audio/mp4",
            headers={"Cache-Control": "private, max-age=0, must-revalidate"},
        )

    return app
