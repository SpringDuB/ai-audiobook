import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import audio, jobs, store
from ..config import OVERLAY_KEYS, get_settings, load_overlay, save_overlay
from ..editing import apply_line_patch, invalidate_chapter
from ..importer import import_book
from ..pipeline import resume_book

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _character_names(settings, book_id: str) -> dict[str, str]:
    payload = store.read_json(store.characters_path(settings, book_id), default={}) or {}
    names = {"narrator": "旁白"}
    for character in payload.get("characters") or []:
        names[character["id"]] = character["name"]
    return names


def _chapter_meta(settings, book_id: str, index: int) -> dict | None:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    for chapter in chapters:
        if int(chapter["index"]) == index:
            return chapter
    return None


def _chapters_with_role(settings, book_id: str, role_id: str) -> list[int]:
    chapters = []
    for path in sorted(store.book_dir(settings, book_id).glob("analysis/lines/chapter_*.jsonl")):
        rows = store.read_jsonl(path)
        if any(row.get("speaker") == role_id or row.get("addressee") == role_id for row in rows):
            chapters.append(int(path.stem.rsplit("_", 1)[-1]))
    return chapters


def _lines_payload(settings, book_id: str, index: int, scene: str | None = None) -> list[dict]:
    rows = store.read_jsonl(store.lines_path(settings, book_id, index))
    clips_dir = store.audio_dir(settings, book_id, index)
    payload = []
    for row in rows:
        if scene and row.get("scene") != scene:
            continue
        clip = clips_dir / f"{row['id']}.wav"
        duration = 0.0
        if clip.exists():
            try:
                duration = round(audio.wav_duration(clip), 3)
            except Exception:  # noqa: BLE001 - 半截文件不该让接口 500
                duration = 0.0
        payload.append(
            {
                **{
                    key: row.get(key)
                    for key in (
                        "id", "seq", "scene", "scene_index", "speaker", "speaker_name",
                        "addressee", "addressee_name", "text", "emotion", "delivery",
                        "pause_after_ms", "rate", "lang",
                    )
                },
                "duration_sec": duration,
                "has_audio": clip.exists(),
                "audio_url": f"/api/books/{book_id}/lines/{row['id']}/audio",
            }
        )
    return payload


def jobs_snapshot(conn, book_id: str | None = None) -> dict:
    """SSE 推送的任务快照（抽成函数便于单测，不必读无限流）。"""
    return {
        "ts": int(time.time() * 1000),
        "jobs": [j.__dict__ for j in jobs.list_jobs(conn, book_id)],
    }


def create_app(settings, conn) -> FastAPI:
    app = FastAPI(title="AI 有声书")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.post("/api/books")
    async def upload_book(file: UploadFile = File(...), title: str = Form("未命名")):
        data = await file.read()
        tmp = settings.data_dir / "uploads" / (file.filename or "book.txt")
        store.atomic_write_bytes(tmp, data)
        book_id = import_book(settings, conn, tmp, title=title)
        return {"book_id": book_id}

    @app.get("/api/books")
    def list_books():
        rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
        return {"books": [{**dict(r), "stats": store.book_stats(settings, r["id"])} for r in rows]}

    @app.get("/api/books/{book_id}/chapters")
    def book_chapters(book_id: str):
        chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
        if not chapters:
            raise HTTPException(status_code=404, detail="book not found")
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
        return {"chapters": payload}

    @app.get("/api/books/{book_id}/chapters/{index}/scenes")
    def chapter_scenes(book_id: str, index: int):
        chapter = _chapter_meta(settings, book_id, index)
        if chapter is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        payload = store.read_json(store.scenes_path(settings, book_id, index), default={}) or {}
        names = _character_names(settings, book_id)
        lines = _lines_payload(settings, book_id, index)
        grouped: dict[str, list[dict]] = {}
        for line in lines:
            grouped.setdefault(line["scene"], []).append(line)
        scenes = []
        for scene in payload.get("scenes") or []:
            bucket = grouped.get(scene["id"], [])
            scenes.append(
                {
                    "id": scene["id"],
                    "index": scene["index"],
                    "title": scene.get("title") or "",
                    "summary": scene.get("summary") or "",
                    "participants": [
                        {"id": pid, "name": names.get(pid, pid)} for pid in scene.get("participants") or []
                    ],
                    "tone": scene.get("tone") or {},
                    "start_line": scene.get("start_line"),
                    "end_line": scene.get("end_line"),
                    "lines": len(bucket),
                    "duration_sec": round(sum(line["duration_sec"] for line in bucket), 2),
                    "audio_ready": bool(bucket) and all(line["has_audio"] for line in bucket),
                }
            )
        return {
            "chapter_index": index,
            "title": chapter.get("title") or "",
            "sentence_count": payload.get("sentence_count") or len(lines),
            "scenes": scenes,
        }

    @app.get("/api/books/{book_id}/chapters/{index}/lines")
    def chapter_lines(book_id: str, index: int, scene: str | None = None):
        if _chapter_meta(settings, book_id, index) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        return {"lines": _lines_payload(settings, book_id, index, scene)}

    @app.get("/api/books/{book_id}/lines/{line_id}/audio")
    def line_audio(book_id: str, line_id: str):
        for chapter_dir in sorted(store.book_dir(settings, book_id).glob("audio/chapter_*")):
            clip = chapter_dir / f"{line_id}.wav"
            if clip.exists():
                return FileResponse(clip, media_type="audio/wav")
        raise HTTPException(status_code=404, detail="audio not ready")

    @app.get("/api/books/{book_id}/issues")
    def book_issues(book_id: str):
        rows = store.read_jsonl(store.issues_path(settings, book_id))
        return {"issues": list(reversed(rows))[:200]}

    @app.get("/api/voices")
    def list_voices():
        voices = []
        for path in sorted(settings.voices_dir.glob("*/voice.json")):
            meta = store.read_json(path, default={}) or {}
            voice_id = meta.get("id") or path.parent.name
            voices.append(
                {
                    "id": voice_id,
                    "name": meta.get("name") or voice_id,
                    "gender": meta.get("gender"),
                    "age_group": meta.get("age_group"),
                    "tags": meta.get("tags") or [],
                    "has_ref": (path.parent / "ref.wav").exists(),
                    "sample_url": f"/api/voices/{voice_id}/sample",
                }
            )
        return {"voices": voices}

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
        roles = [
            {**role, "name": role.get("name") or names.get(role_id, role_id)}
            for role_id, role in (casting.get("roles") or {}).items()
        ]
        voices = []
        for path in sorted(settings.voices_dir.glob("*/voice.json")):
            meta = store.read_json(path, default={}) or {}
            voices.append({"id": path.parent.name, "name": meta.get("name") or path.parent.name})
        return {"narrator_voice": casting.get("narrator_voice") or "default", "roles": roles, "voices": voices}

    @app.get("/api/settings")
    def read_settings():
        fresh = _fresh_settings()
        payload = {key: getattr(settings, key) for key in OVERLAY_KEYS}
        payload.update({key: getattr(fresh, key) for key in OVERLAY_KEYS})
        payload["llm_api_key_set"] = bool(fresh.llm_api_key)
        return {"settings": payload, "overlay_keys": sorted(load_overlay(fresh))}

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
        casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
        roles = casting.setdefault("roles", {})
        if role_id not in roles:
            raise HTTPException(status_code=404, detail="role not found")
        roles[role_id]["voice_id"] = str(payload.get("voice_id") or roles[role_id].get("voice_id") or "default")
        roles[role_id]["overrides"] = payload.get("overrides") or roles[role_id].get("overrides") or {}
        roles[role_id]["source"] = "manual"
        store.atomic_replace_json(store.casting_path(settings, book_id), casting)
        chapters = _chapters_with_role(settings, book_id, role_id)
        for index in chapters:
            invalidate_chapter(settings, book_id, index)
        return {"role": roles[role_id], "invalidated": chapters}

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
        }

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str):
        meta = store.read_json(store.book_dir(settings, book_id) / "book.json")
        if meta is None:
            raise HTTPException(status_code=404, detail="book not found")
        chapters = store.read_json(store.chapters_path(settings, book_id), default={})
        return {"book": meta, "chapters": chapters}

    @app.post("/api/books/{book_id}/run")
    def run_book(book_id: str):
        plan = resume_book(settings, conn, book_id)
        return {"ok": True, "queued": len(plan), "plan": plan}

    @app.get("/api/jobs")
    def list_jobs(book_id: str | None = None):
        return {"jobs": [j.__dict__ for j in jobs.list_jobs(conn, book_id)]}

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

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        jobs.request_cancel(conn, job_id)
        return {"ok": True}

    @app.get("/api/events")
    async def events(book_id: str | None = None):
        async def gen():
            last = None
            while True:
                payload = json.dumps(jobs_snapshot(conn, book_id), ensure_ascii=False)
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/books/{book_id}/chapters/{index}/audio")
    def chapter_audio(book_id: str, index: int):
        path = store.output_dir(settings, book_id) / f"chapter_{store.chapter_tag(index)}.wav"
        if not path.exists():
            raise HTTPException(status_code=404, detail="audio not ready")
        return FileResponse(path, media_type="audio/wav")

    return app
