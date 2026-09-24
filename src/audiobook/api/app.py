import asyncio
import json
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .. import jobs, store
from ..importer import import_book
from ..pipeline import resume_book


def jobs_snapshot(conn, book_id: str | None = None) -> dict:
    """SSE 推送的任务快照（抽成函数便于单测，不必读无限流）。"""
    return {
        "ts": int(time.time() * 1000),
        "jobs": [j.__dict__ for j in jobs.list_jobs(conn, book_id)],
    }


def create_app(settings, conn) -> FastAPI:
    app = FastAPI(title="AI 有声书")

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
        return {"books": [dict(r) for r in rows]}

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
