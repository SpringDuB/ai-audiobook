"""手机听书：m4a 懒转码、逐句时间轴、听书目录与 API。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiobook import listen, store
from audiobook.api.app import create_app
from audiobook.db import connect, init_db
from audiobook.render.chapter import render_chapter
from fake_engine import FakeEngine
from helpers import requires_ffmpeg


def _seed_rendered_book(settings, book_id="b1", chapter=1, text="第一句。第二句。"):
    """一本书、一章、逐句音频、以及真实的 wav/srt/render.json 产物。"""
    store.atomic_replace_json(store.book_dir(settings, book_id) / "book.json", {"id": book_id, "title": "测试书"})
    store.atomic_replace_json(
        store.chapters_path(settings, book_id),
        {"chapters": [{"index": chapter, "title": "第一章 起风", "content": text, "chars": len(text)}]},
    )
    from conftest import make_narrator_lines

    rows = make_narrator_lines(chapter, text)
    if len(rows) >= 2:
        rows[1] = {**rows[1], "speaker": "c002", "speaker_name": "张卫东", "kind": "dialogue"}
    store.write_jsonl_atomic(store.lines_path(settings, book_id, chapter), rows)
    engine = FakeEngine(ms_per_char=10.0)
    for row in rows:
        engine.synthesize(row["text"], "default", None, store.audio_dir(settings, book_id, chapter) / f"{row['id']}.wav")
    render_chapter(settings, book_id, chapter)
    return rows


def test_catalog_lists_only_rendered_chapters(settings):
    _seed_rendered_book(settings)
    store.atomic_replace_json(
        store.chapters_path(settings, "b1"),
        {
            "chapters": [
                {"index": 1, "title": "第一章 起风", "content": "第一句。第二句。", "chars": 8},
                {"index": 2, "title": "第二章 未录", "content": "还没合成。", "chars": 6},
            ]
        },
    )
    payload = listen.catalog(settings, "b1")
    assert payload["title"] == "测试书"
    assert [chapter["index"] for chapter in payload["chapters"]] == [1]
    chapter = payload["chapters"][0]
    assert chapter["cues"] == 2
    assert chapter["duration"] == pytest.approx(0.10, abs=1e-2)
    assert chapter["m4a_ready"] is False
    assert chapter["estimated_bytes"] > 0


def test_timeline_maps_speaker_and_skips_lines_without_audio(settings):
    _seed_rendered_book(settings)
    timeline = listen.chapter_timeline(settings, "b1", 1)
    assert [cue["speaker_name"] for cue in timeline["cues"]] == ["旁白", "张卫东"]
    assert timeline["cues"][0]["start"] == pytest.approx(0.0)
    assert timeline["cues"][1]["start"] == pytest.approx(0.05, abs=1e-2)
    assert timeline["audio"]["m4a_ready"] is False
    assert timeline["title"] == "第一章 起风"

    # 第二句没有音频：SRT 只剩一条，说话人按顺序对齐，不会张冠李戴
    (store.audio_dir(settings, "b1", 1) / "c0001-s01-l002.wav").unlink()
    render_chapter(settings, "b1", 1, force=True)
    timeline = listen.chapter_timeline(settings, "b1", 1)
    assert len(timeline["cues"]) == 1
    assert timeline["cues"][0]["speaker_name"] == "旁白"


def test_timeline_404_when_chapter_not_rendered(settings):
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "空书"})
    with pytest.raises(FileNotFoundError):
        listen.chapter_timeline(settings, "b1", 1)


@requires_ffmpeg
def test_ensure_mobile_audio_transcodes_and_reuses(settings):
    _seed_rendered_book(settings)
    path = listen.ensure_mobile_audio(settings, "b1", 1)
    assert path.exists() and path.stat().st_size > 0
    first = path.stat().st_mtime_ns
    assert listen.ensure_mobile_audio(settings, "b1", 1) == path
    assert path.stat().st_mtime_ns == first  # 没变就不重复转码

    wav = store.chapter_wav_path(settings, "b1", 1)
    import os
    import time

    # 把 m4a 做旧：wav 相对更新 → 下次访问必须重转
    past = time.time() - 60
    os.utime(path, (past, past))
    assert wav.stat().st_mtime > path.stat().st_mtime
    listen.ensure_mobile_audio(settings, "b1", 1)
    assert path.stat().st_mtime_ns != first
    assert listen.catalog(settings, "b1")["chapters"][0]["m4a_ready"] is True


def test_mobile_encoder_setting_controls_ffmpeg_args(settings, monkeypatch):
    """编码器按设置传：aac_mf 不认 -profile:a，得单独分支，别把参数硬塞给它。"""
    _seed_rendered_book(settings)
    calls: list[list[str]] = []

    def fake_run(_settings, args, **_kwargs):
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42")  # 假装转码产物已落盘
        return ""

    monkeypatch.setattr(listen, "run_ffmpeg", fake_run)

    listen.ensure_mobile_audio(settings, "b1", 1)
    assert calls[-1][calls[-1].index("-c:a") + 1] == "aac"
    assert "-profile:a" in calls[-1]
    assert calls[-1][calls[-1].index("-b:a") + 1] == "64k"

    hardware = settings.model_copy(update={"mobile_audio_encoder": "aac_mf"})
    listen.ensure_mobile_audio(hardware, "b1", 1, force=True)
    assert calls[-1][calls[-1].index("-c:a") + 1] == "aac_mf"
    assert "-profile:a" not in calls[-1]


@requires_ffmpeg
def test_listen_api_end_to_end(settings):
    _seed_rendered_book(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO books(id, title, source_path, chapter_count, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        ("b1", "测试书", "source/original.txt", 1, "imported", 1790000000000),
    )
    client = TestClient(create_app(settings, conn))

    catalog = client.get("/api/books/b1/listen").json()
    assert catalog["chapters"][0]["index"] == 1
    assert client.get("/api/books/nope/listen").status_code == 404

    subtitles = client.get("/api/books/b1/chapters/1/subtitles")
    assert subtitles.status_code == 200
    assert len(subtitles.json()["cues"]) == 2
    assert client.get("/api/books/b1/chapters/9/subtitles").status_code == 404

    prepared = client.post("/api/books/b1/chapters/1/mobile", json={})
    assert prepared.status_code == 200 and prepared.json()["ok"] is True

    audio = client.get("/api/books/b1/chapters/1/audio.m4a")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mp4"
    # 手机播放器靠 Range 拖动进度，必须给 206 而不是整段 200
    ranged = client.get("/api/books/b1/chapters/1/audio.m4a", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206
    assert ranged.headers["content-range"].startswith("bytes 0-99/")
    conn.close()
