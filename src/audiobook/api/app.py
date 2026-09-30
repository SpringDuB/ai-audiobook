import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import audio, jobs, store, voicelib
from ..analysis.casting import voice_for_speaker
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
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    for chapter in chapters:
        if int(chapter["index"]) == index:
            return chapter
    return None


def _role_occurrences(settings, book_id: str) -> dict[str, dict]:
    """扫一遍逐句标注，统计每个角色出现在哪些章节、共多少句。"""
    result: dict[str, dict] = {}
    for path in sorted(store.book_dir(settings, book_id).glob("analysis/lines/chapter_*.jsonl")):
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
    return {role_id: {"chapters": sorted(bucket["chapters"]), "lines": bucket["lines"]} for role_id, bucket in result.items()}


def _chapters_with_role(settings, book_id: str, role_id: str) -> list[int]:
    return _role_occurrences(settings, book_id).get(role_id, {}).get("chapters", [])


def _casting_covers_chapter(settings, book_id: str, index: int) -> bool:
    """这一章的每个说话人都已经绑定音色了吗？（没有就要先补一轮选角）"""
    casting = store.read_json(store.casting_path(settings, book_id), default={}) or {}
    if not (casting.get("roles") or {}):
        return False
    for row in store.read_jsonl(store.lines_path(settings, book_id, index)):
        speaker = row.get("speaker")
        if speaker and not voice_for_speaker(casting, speaker):
            return False
    return True


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
                        "addressee", "addressee_name", "text", "emotion", "delivery",
                        "rate", "lang",
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


def jobs_snapshot(conn, book_id: str | None = None) -> dict:
    """SSE 推送的任务快照（抽成函数便于单测，不必读无限流）。"""
    return {
        "ts": int(time.time() * 1000),
        "jobs": [j.__dict__ for j in jobs.list_jobs(conn, book_id)],
    }


def create_app(settings, conn) -> FastAPI:
    app = FastAPI(title="AI 有声书")
    tts_service = LocalTtsService(settings)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html", media_type="text/html")

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
        chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
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
            {
                **role,
                "name": role.get("name") or names.get(role_id, role_id),
                "chapters": occurrences.get(role_id, {}).get("chapters", []),
                "lines": occurrences.get(role_id, {}).get("lines", 0),
            }
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
        voice_name = payload.get("voice_name")
        if voice_name:
            roles[role_id]["voice_name"] = str(voice_name)
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
            "emotion_text_enabled": EMOTION_TEXT_ENABLED,
        }

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str):
        meta = store.read_json(store.book_dir(settings, book_id) / "book.json")
        if meta is None:
            raise HTTPException(status_code=404, detail="book not found")
        chapters = store.read_json(store.chapters_path(settings, book_id), default={})
        return {"book": meta, "chapters": chapters, "output": _output_info(settings, book_id)}

    @app.post("/api/books/{book_id}/run")
    def run_book(book_id: str):
        plan = resume_book(settings, conn, book_id)
        return {"ok": True, "queued": len(plan), "plan": plan}

    @app.post("/api/books/{book_id}/analyze")
    def analyze_book(book_id: str, force: bool = False):
        """只推分析链：分章 → 整章分析（角色 + 逐句情感）→ 选角。

        force=true 时无视断点整本重跑，会覆盖已分析的逐句标注（含人工修改）。
        """
        plan = resume_book(settings, conn, book_id, phase="analysis", force=force)
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
        """只重跑勾选章节的分析：删掉这些章的提取结果，再重新提取。

        勾选全部章节时走整书 characters 任务（跨章合并同人异名更准，跑完会自动
        为每章排队 lines）；勾选一部分时走一个 chapters 批量任务：job 内部按大模型
        并发同时提这几章（和 characters 同一模式），新称呼并进现有角色表。
        """
        try:
            picked = sorted({int(item) for item in ((payload or {}).get("chapters") or [])})
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="chapters 必须是章节序号数组") from exc
        if not picked:
            raise HTTPException(status_code=400, detail="至少选择一章")
        chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
        known = {int(chapter["index"]) for chapter in chapters}
        if not known:
            raise HTTPException(status_code=409, detail="还没有分章结果，请先分章")
        unknown = [index for index in picked if index not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"不存在的章节：{unknown}")
        if len(picked) == len(known):
            for index in picked:
                store.extract_path(settings, book_id, index).unlink(missing_ok=True)
            plan: list[tuple[str, int | None]] = [("characters", None)]
            job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
            return {"ok": True, "queued": len(job_ids), "plan": plan, "job_ids": job_ids}

        active = jobs.find_active(conn, "chapters", book_id)
        if active is not None and active.status == "running":
            # 已经在跑的批量任务只认它开始时那份清单：这次勾选里它没覆盖的章退回按章
            # lines（各章一个 job），保证不会静默丢单。它已覆盖的章交给它自己跑。
            covered = {int(item) for item in ((active.payload or {}).get("chapters") or [])}
            extra = [index for index in picked if index not in covered]
            for index in extra:
                store.extract_path(settings, book_id, index).unlink(missing_ok=True)
            plan = [("lines", index) for index in extra]
            job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
            return {
                "ok": True,
                "queued": len(job_ids),
                "plan": plan,
                "job_ids": job_ids,
                "running_job_id": active.id,
            }

        for index in picked:
            store.extract_path(settings, book_id, index).unlink(missing_ok=True)
        if active is not None:
            # 还没开跑：把这次勾选并进同一条批量任务，避免两次点击各跑一批
            merged = sorted({int(item) for item in ((active.payload or {}).get("chapters") or [])} | set(picked))
            jobs.set_payload(conn, active.id, {"chapters": merged})
            return {"ok": True, "queued": 1, "plan": [["chapters", None]], "job_ids": [active.id]}

        job_id = jobs.enqueue(conn, "chapters", book_id, payload={"chapters": picked})
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
            # 角色还没选音色：先补一轮选角，再合成这一章
            plan.append(("casting", None))
        plan.append(("synthesize", index))
        job_ids = [jobs.enqueue(conn, kind, book_id, chapter_index) for kind, chapter_index in plan]
        return {"ok": True, "queued": len(job_ids), "plan": plan, "job_ids": job_ids}

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
