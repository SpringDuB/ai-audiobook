import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs as jobs_mod
from .. import store
from ..analysis.casting import voice_for_speaker
from ..analysis.issues import record_issue
from ..cache import cache_key, params_from_line
from ..engines.errors import TtsUnavailable, TtsVoiceMissing
from ..worker import register

logger = logging.getLogger(__name__)


def effective_concurrency(ctx) -> int:
    """TTS 并发上限来自服务端自报；客户端只保留安全上限。

    优先用"总容量"而不是"此刻剩余空位"：工作线程数在任务开始时定死，
    拿剩余空位会把整章锁在低并发（真正的节流由 TTS 池逐个请求把关）。
    """
    for name in ("capacity_hint", "concurrency_hint"):
        hint = getattr(ctx.engine, name, None)
        if not callable(hint):
            continue
        reported = hint()
        if reported and reported > 0:
            return max(1, min(ctx.settings.synth_concurrency_max, int(reported)))
    return max(1, ctx.settings.synth_concurrency)


def resolve_voice_id(settings, book_id: str, speaker: str) -> str:
    casting = store.read_json(store.casting_path(settings, book_id), default={})
    if not isinstance(casting, dict) or not casting:
        return "default"
    return voice_for_speaker(casting, speaker) or "default"


def line_target(ctx, job, row: dict) -> dict:
    """一行要用的音色 / 参数 / 文件路径 / 缓存键（单条与批量共用同一份计算）。"""
    caps = ctx.engine.capabilities()
    voice_id = resolve_voice_id(ctx.settings, job.book_id, row["speaker"])
    params = params_from_line(row, caps, mode=ctx.settings.emotion_mode)
    key = cache_key(row["text"], voice_id, caps, params)
    clip = store.audio_dir(ctx.settings, job.book_id, job.chapter_index) / f"{row['id']}.wav"
    meta_path = clip.with_suffix(".meta.json")
    return {
        "voice_id": voice_id,
        "params": params,
        "key": key,
        "clip": clip,
        "meta_path": meta_path,
        "meta": store.read_json(meta_path),
    }


def line_cached(target: dict) -> bool:
    """这一行能不能直接复用（缓存键一致且音频文件还在）。"""
    meta = target["meta"]
    return bool(meta and target["clip"].exists() and meta.get("cache_key") == target["key"])


def write_line_meta(ctx, job, row: dict, target: dict, duration: float) -> None:
    store.atomic_replace_json(
        target["meta_path"],
        {
            "id": row["id"],
            "cache_key": target["key"],
            "voice_id": target["voice_id"],
            "engine": ctx.engine.capabilities().name,
            "engine_version": ctx.engine.capabilities().version,
            "params": {
                "emo_vector": list(target["params"].emo_vector) if target["params"].emo_vector else None,
                "emotion_text": target["params"].emotion_text,
                "emotion_mode": ctx.settings.emotion_mode,
                "rate": target["params"].rate,
                "lang": target["params"].lang,
                "pronunciation": target["params"].pronunciation,
            },
            "duration": duration,
            "sample_rate": ctx.engine.capabilities().sample_rate,
        },
    )


def synth_line(ctx, job, row: dict, target: dict | None = None) -> dict:
    """合成（或复用）一行音频，返回 {"id", "cached", "duration"}。"""
    target = target or line_target(ctx, job, row)
    if line_cached(target):
        return {"id": row["id"], "cached": True, "duration": target["meta"]["duration"]}
    result = ctx.engine.synthesize(row["text"], target["voice_id"], target["params"], target["clip"])
    write_line_meta(ctx, job, row, target, result.duration)
    return {"id": row["id"], "cached": False, "duration": result.duration}


def batch_plan(ctx, rows: list[dict]) -> tuple[int, int]:
    """(每包几条, 几个包并排跑)。服务端不支持批量就退回逐行 + 原来的并发。"""
    size = max(1, int(getattr(ctx.settings, "synth_batch_size", 1) or 1))
    caps = ctx.engine.capabilities()
    if size <= 1 or not getattr(caps, "batch", False) or not hasattr(ctx.engine, "synthesize_batch"):
        return 1, effective_concurrency(ctx)
    size = min(size, max(1, int(getattr(caps, "max_batch_items", 1) or 1)), max(1, len(rows)))
    workers = max(1, int(getattr(ctx.settings, "synth_batch_workers", 1) or 1))
    return size, workers


def group_batches(rows: list[dict], targets: dict, size: int, chunk_limit: int) -> list[list[dict]]:
    """按音色打包：同一个音色的行排在一起，每 size 条一包。

    太长的文本（客户端本来就要分块）单独成包，走原来的逐行路径，时序与以前一致。
    包内按文本长度排序：批量解码会把一个包补齐到最长那条，长短混在一起会白白多算。
    """
    groups: list[list[dict]] = []
    by_voice: dict[str, list[dict]] = {}
    for row in rows:
        if len(row["text"]) > chunk_limit:
            groups.append([row])
            continue
        by_voice.setdefault(targets[row["id"]]["voice_id"], []).append(row)
    for voice_rows in by_voice.values():
        ordered = sorted(voice_rows, key=lambda row: len(row["text"]))
        for start in range(0, len(ordered), size):
            groups.append(ordered[start : start + size])
    return groups


@register("synthesize")
def handle_synthesize(ctx, job) -> None:
    rows = store.read_jsonl(store.lines_path(ctx.settings, job.book_id, job.chapter_index))
    if not rows:
        raise RuntimeError(f"第 {job.chapter_index} 章没有行数据，请先跑 chapter_split")
    total = len(rows)
    done = 0
    failed = 0
    endpoint_down = False

    targets = {row["id"]: line_target(ctx, job, row) for row in rows}
    pending = [row for row in rows if not line_cached(targets[row["id"]])]
    batch_size, workers = batch_plan(ctx, pending)
    chunk_limit = ctx.settings.tts_max_line_chunk_chars or ctx.engine.capabilities().max_text_chars
    groups = group_batches(pending, targets, batch_size, chunk_limit)

    # 正在跑的行号：前端拿它给对应段落画"生成中"沙漏。行合成好就落盘，
    # 所以前端只要盯着 has_audio 翻转就能实时把试听按钮点亮点出来。
    inflight: set[str] = set()
    inflight_lock = threading.Lock()

    def note_progress(row: dict) -> None:
        with inflight_lock:
            current = sorted(inflight)
        ctx.progress(job, done, total, row["id"], extra={"inflight": current})

    def record_failure(row: dict, exc: Exception) -> None:
        nonlocal failed, endpoint_down
        failed += 1
        kind = "tts_line_failed"
        if isinstance(exc, TtsVoiceMissing):
            kind = "tts_ref_missing"
        if isinstance(exc, TtsUnavailable):
            endpoint_down = True
        logger.warning("行 %s 合成失败: %s", row["id"], exc)
        record_issue(
            ctx.settings,
            job.book_id,
            kind,
            reason=f"{type(exc).__name__}: {exc}",
            chapter=job.chapter_index,
            line=row["id"],
            fallback="该行留空，post 会跳过并记 audio_missing",
            detail={"text": row["text"]},
        )

    def run_single(row: dict) -> dict:
        with inflight_lock:
            inflight.add(row["id"])
        try:
            return synth_line(ctx, job, row, targets[row["id"]])
        finally:
            with inflight_lock:
                inflight.discard(row["id"])

    def run_batch(group: list[dict]) -> None:
        ids = [row["id"] for row in group]
        with inflight_lock:
            inflight.update(ids)
        try:
            items = [(row["text"], targets[row["id"]]["params"]) for row in group]
            out_paths = [targets[row["id"]]["clip"] for row in group]
            results = ctx.engine.synthesize_batch(items, targets[ids[0]]["voice_id"], out_paths)
            for row, result in zip(group, results):
                write_line_meta(ctx, job, row, targets[row["id"]], result.duration)
            logger.info("批量合成 %d 条完成（音色 %s）", len(group), targets[ids[0]]["voice_id"])
        finally:
            with inflight_lock:
                inflight.difference_update(ids)

    # 已经在缓存里的行先算完：前端一进来就能看到"已合成 x/y"
    done += total - len(pending)
    ctx.progress(job, done, total, "", extra={"inflight": []})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for group in groups:
            task = run_batch if len(group) > 1 else run_single
            argument = group if len(group) > 1 else group[0]
            futures[pool.submit(task, argument)] = group
        for future in as_completed(futures):
            group = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - 单行失败不能拖垮整章
                # 整包失败：退回逐行重试，尽量只让真正坏的那几行失败
                if len(group) > 1:
                    logger.warning("批量合成 %d 条失败（%s），退回逐行重试", len(group), exc)
                for row in group:
                    if len(group) > 1:
                        try:
                            run_single(row)
                        except Exception as single_exc:  # noqa: BLE001
                            record_failure(row, single_exc)
                    else:
                        record_failure(row, exc)
                    done += 1
                    note_progress(row)
                continue
            for row in group:
                done += 1
                note_progress(row)
    if failed == total:
        if endpoint_down:
            record_issue(
                ctx.settings,
                job.book_id,
                "tts_endpoint_down",
                reason="所有 TTS 端点不可用",
                chapter=job.chapter_index,
                fallback="任务退避后重试整章",
            )
            raise RuntimeError("TTS 端点全部不可用，退避后重试")
        raise RuntimeError("整章所有行都合成失败")
    ctx.progress(job, total, total, f"完成，失败 {failed} 行")
    jobs_mod.enqueue(ctx.conn, "post", job.book_id, job.chapter_index)
