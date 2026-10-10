import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

_BOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 多线程（整章并发分析）会同时写 logs/llm.jsonl 与 issues.jsonl：
# Windows 上文本模式追加写不是原子的，会互相覆盖丢行，所以串行化并重试。
_APPEND_LOCK = threading.Lock()
# 章节状态缓存：界面每切一次页都要问一遍全书章节状态（一本 256 章的书 = 2.3 万行），
# 键是 (lines.jsonl、片段目录、render.json、章节 wav) 的 mtime/size —— worker 一写文件，
# 签名就变，缓存自动失效，不需要任何 TTL 猜测。
_STATE_CACHE_LOCK = threading.Lock()
_STATE_CACHE: dict[tuple[str, str, int], tuple[tuple, dict]] = {}
_STATE_CACHE_MAX = 20000


def file_signature(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size)；文件/目录不存在返回 None。用于"内容变没变"的廉价判定。"""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _clip_ids(directory: Path) -> set[str]:
    """列一次目录拿到已存在的片段 id。

    旧写法是对每一行做一次 ``(clips_dir / id.wav).exists()``：一本 256 章的书就是
    2.3 万次 stat，光这一项就让 /api/books 慢到 3~5 秒。扫描目录一次就够。
    """
    try:
        entries = os.scandir(directory)
    except OSError:
        return set()
    with entries:
        return {entry.name[:-4] for entry in entries if entry.name.endswith(".wav")}


def book_dir(settings, book_id: str) -> Path:
    return settings.books_dir / book_id


def valid_book_id(book_id: str) -> bool:
    """book_id 会拼进文件路径，删除这类破坏性操作前必须挡住 ../ 之类的东西。"""
    return bool(_BOOK_ID_RE.match(book_id or ""))


def running_job_count(conn, book_id: str, now: int | None = None) -> int:
    """真正在跑（租约未过期）的任务数。

    租约过期的 running 行是崩溃 worker 留下的残骸，不该把删除永久卡住。
    """
    if conn is None:
        return 0
    ts = int(time.time() * 1000) if now is None else now
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs"
        " WHERE book_id=? AND status='running' AND (lease_expires_at IS NULL OR lease_expires_at > ?)",
        (book_id, ts),
    ).fetchone()
    return int(row["n"])


def delete_book(settings, conn, book_id: str) -> dict:
    """删除一本书：任务行 + 书籍行 + 整个书籍目录。

    调用方负责先确认没有运行中的任务（见 running_job_count），否则 worker
    会在目录被删后继续写文件，留下无主残骸。
    """
    if not valid_book_id(book_id):
        raise ValueError(f"非法书籍 id: {book_id!r}")
    target = book_dir(settings, book_id)
    books_root = settings.books_dir.resolve()
    resolved = target.resolve()
    if resolved == books_root or books_root not in resolved.parents:
        raise ValueError(f"书籍目录越界: {resolved}")

    row = conn.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
    title = row["title"] if row is not None else None
    removed_jobs = conn.execute("DELETE FROM jobs WHERE book_id=?", (book_id,)).rowcount
    conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    dir_removed = target.exists()
    if dir_removed:
        shutil.rmtree(target)
    return {
        "book_id": book_id,
        "title": title,
        "removed_jobs": max(0, int(removed_jobs)),
        "dir_removed": dir_removed,
    }


def chapter_tag(index: int) -> str:
    return f"{index:04d}"


def source_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "source" / "original.txt"


def chapters_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "chapters.json"


def lines_path(settings, book_id: str, index: int) -> Path:
    return book_dir(settings, book_id) / "analysis" / "lines" / f"chapter_{chapter_tag(index)}.jsonl"


def extract_path(settings, book_id: str, index: int) -> Path:
    """整章提取原始结果（每句话 + 说话人 + 对白情绪，说话人还是名字不是 role_id）。"""
    return book_dir(settings, book_id) / "analysis" / "extract" / f"chapter_{chapter_tag(index)}.json"


def audio_dir(settings, book_id: str, index: int) -> Path:
    return book_dir(settings, book_id) / "audio" / f"chapter_{chapter_tag(index)}"


def output_dir(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "output"


def issues_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "issues.jsonl"


def logs_dir(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "logs"


def llm_log_path(settings, book_id: str) -> Path:
    return logs_dir(settings, book_id) / "llm.jsonl"


def characters_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "analysis" / "characters.json"


def casting_path(settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "voices" / "casting.json"


def role_voice_dir(settings, book_id: str, role_id: str) -> Path:
    """角色设计音色的目录（VoiceDesign 造出来的参考音频就放这儿，跟着书走）。"""
    return book_dir(settings, book_id) / "voices" / role_id


def role_design_path(settings, book_id: str, role_id: str) -> Path:
    """角色音色设计档案：描述文本 + 试音台词 + 版本号（前端改描述/重生成都看它）。"""
    return role_voice_dir(settings, book_id, role_id) / "design.json"


def role_ref_path(settings, book_id: str, role_id: str) -> Path:
    """角色设计出来的参考音频（克隆全文用的那一段）。"""
    return role_voice_dir(settings, book_id, role_id) / "ref.wav"


def voice_library_dir(settings) -> Path:
    return settings.voices_dir


def voice_path(settings, voice_id: str) -> Path:
    return settings.voices_dir / voice_id / "voice.json"


def voice_ref_path(settings, voice_id: str) -> Path:
    return settings.voices_dir / voice_id / "ref.wav"


def pronounce_path(settings) -> Path:
    return settings.data_dir / "pronounce.json"


def chapter_wav_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.wav"


def chapter_srt_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.srt"


def chapter_media_path(settings, book_id: str, index: int, ext: str) -> Path:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}{suffix}"


def chapter_render_meta_path(settings, book_id: str, index: int) -> Path:
    return output_dir(settings, book_id) / f"chapter_{chapter_tag(index)}.render.json"


def render_work_dir(settings, book_id: str, index: int) -> Path:
    return audio_dir(settings, book_id, index) / "_render"


def book_wav_path(settings, book_id: str) -> Path:
    return output_dir(settings, book_id) / "book.wav"


def book_srt_path(settings, book_id: str) -> Path:
    return output_dir(settings, book_id) / "book.srt"


def book_media_path(settings, book_id: str, ext: str) -> Path:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return output_dir(settings, book_id) / f"book{suffix}"


def export_target_dir(settings, book_id: str, out_dir=None) -> Path:
    return Path(out_dir) if out_dir else output_dir(settings, book_id)


def settings_overlay_path(settings) -> Path:
    return settings.data_dir / "settings.json"


def count_issues(settings, book_id: str) -> int:
    return len(read_jsonl(issues_path(settings, book_id)))


_CHAPTERS_CACHE_LOCK = threading.Lock()
_CHAPTERS_CACHE: dict[str, tuple[tuple[int, int] | None, dict]] = {}
_CHAPTERS_CACHE_MAX = 8


def read_chapters(settings, book_id: str) -> dict:
    """读 chapters.json（整本书的正文都在里面，实测单本 8MB），按文件签名缓存。

    书架/章节列表/看原文每次刷新都要它，重解析一遍 8MB JSON 就是几十毫秒。
    返回的是缓存对象本身：调用方只读，需要改请自己 copy。
    """
    path = chapters_path(settings, book_id)
    signature = file_signature(path)
    cache_key = f"{settings.data_dir}|{book_id}"
    with _CHAPTERS_CACHE_LOCK:
        cached = _CHAPTERS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    payload = read_json(path, default={}) or {}
    with _CHAPTERS_CACHE_LOCK:
        if len(_CHAPTERS_CACHE) >= _CHAPTERS_CACHE_MAX:
            _CHAPTERS_CACHE.clear()  # 书架规模远小于这个量级，真到上限就整体重来
        _CHAPTERS_CACHE[cache_key] = (signature, payload)
    return payload


def chapter_list(settings, book_id: str) -> list[dict]:
    """chapters.json 里的章节数组（含正文，缓存对象，只读）。"""
    return read_chapters(settings, book_id).get("chapters") or []


def chapter_index(settings, book_id: str) -> list[dict]:
    """章节列表的轻量视图：只带 index/title/chars，不带正文。

    书架统计和章节列表只需要这些字段；正文只在用户点开某一章看原文时才要。
    """
    return [
        {
            "index": int(chapter["index"]),
            "title": chapter.get("title") or f"第{int(chapter['index'])}章",
            "chars": chapter.get("chars"),
        }
        for chapter in chapter_list(settings, book_id)
    ]


def chapter_state(settings, book_id: str, index: int) -> dict:
    """章节在流水线上的位置：empty → analyzed → synthesized → rendered。

    这是最热的读路径（书架上每一本书的每一章都要问一次），两级省开销：
    - 片段数用一次 ``os.scandir`` 数出来，不再对每一行做 ``exists()``；
    - 结果按四个文件的 mtime/size 缓存，文件没动过就直接命中（worker 写文件会改签名）。
    """
    index = int(index)
    lines = lines_path(settings, book_id, index)
    clips_dir = audio_dir(settings, book_id, index)
    meta_path = chapter_render_meta_path(settings, book_id, index)
    wav_path = chapter_wav_path(settings, book_id, index)
    signature = (
        file_signature(lines),
        file_signature(clips_dir),
        file_signature(meta_path),
        file_signature(wav_path),
    )
    cache_key = (str(settings.data_dir), book_id, index)
    with _STATE_CACHE_LOCK:
        cached = _STATE_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return dict(cached[1])

    rows = read_jsonl(lines)
    meta = read_json(meta_path, default={}) or {}
    present = _clip_ids(clips_dir)
    segments = sum(1 for row in rows if row.get("id") in present)
    if meta and signature[3] is not None:
        state = "rendered"
    elif segments:
        state = "synthesized"
    elif rows:
        state = "analyzed"
    else:
        state = "empty"
    payload = {
        "index": index,
        "lines": len(rows),
        "segments": segments,
        "duration_sec": float(meta.get("duration") or 0.0),
        "rendered_at": meta.get("generated_at"),
        "state": state,
    }
    with _STATE_CACHE_LOCK:
        if len(_STATE_CACHE) >= _STATE_CACHE_MAX:
            _STATE_CACHE.clear()  # 章节数远小于这个量级；真到上限就整体重来，不做 LRU
        _STATE_CACHE[cache_key] = (signature, dict(payload))
    return payload


def _analysis_in_flight(conn, book_id: str) -> bool:
    """分析链里还有排队/运行中的任务吗？拿不到连接（纯文件视角）时按"没有"处理。"""
    if conn is None:
        return False
    from .pipeline import ANALYSIS_KINDS  # 延迟导入：pipeline 依赖 store，模块级导入会成环

    rows = conn.execute(
        "SELECT kind FROM jobs WHERE book_id=? AND status IN ('queued','running')", (book_id,)
    ).fetchall()
    return any(row["kind"] in ANALYSIS_KINDS for row in rows)


def book_stats(settings, book_id: str, conn=None) -> dict:
    chapters = chapter_index(settings, book_id)
    total = len(chapters)
    analyzed = generated = 0
    duration = 0.0
    for chapter in chapters:
        detail = chapter_state(settings, book_id, int(chapter["index"]))
        analyzed += 1 if detail["lines"] else 0
        generated += 1 if detail["state"] == "rendered" else 0
        duration += detail["duration_sec"]
    if total == 0:
        state = "empty"
    elif generated == total:
        state = "ready"
    elif generated:
        state = "synthesizing"
    elif analyzed:
        state = "analyzed"
    elif _analysis_in_flight(conn, book_id):
        state = "analyzing"
    else:
        # 分章好了但还没点「一键分析」：别显示成"分析中"
        state = "split"
    return {
        "chapters": total,
        "analyzed": analyzed,
        "generated": generated,
        "duration_sec": round(duration, 2),
        "issues": count_issues(settings, book_id),
        "state": state,
    }


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_replace_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl_atomic(path: Path, rows) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    atomic_write_text(path, text)


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _APPEND_LOCK:
        for attempt in range(3):
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                return
            except PermissionError:  # 另一个进程/线程正开着这个文件
                if attempt == 2:
                    raise
                time.sleep(0.05)
