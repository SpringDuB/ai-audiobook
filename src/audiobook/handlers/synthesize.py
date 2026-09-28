import logging
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


def synth_line(ctx, job, row: dict) -> dict:
    """合成（或复用）一行音频，返回 {"id", "cached", "duration"}。"""
    caps = ctx.engine.capabilities()
    voice_id = resolve_voice_id(ctx.settings, job.book_id, row["speaker"])
    params = params_from_line(row, caps, mode=ctx.settings.emotion_mode)
    key = cache_key(row["text"], voice_id, caps, params)
    clip = store.audio_dir(ctx.settings, job.book_id, job.chapter_index) / f"{row['id']}.wav"
    meta_path = clip.with_suffix(".meta.json")
    meta = store.read_json(meta_path)
    if meta and clip.exists() and meta.get("cache_key") == key:
        return {"id": row["id"], "cached": True, "duration": meta["duration"]}
    result = ctx.engine.synthesize(row["text"], voice_id, params, clip)
    store.atomic_replace_json(
        meta_path,
        {
            "id": row["id"],
            "cache_key": key,
            "voice_id": voice_id,
            "engine": caps.name,
            "engine_version": caps.version,
            "params": {
                "emo_vector": list(params.emo_vector) if params.emo_vector else None,
                "emotion_text": params.emotion_text,
                "emotion_mode": ctx.settings.emotion_mode,
                "rate": params.rate,
                "lang": params.lang,
                "pronunciation": params.pronunciation,
            },
            "duration": result.duration,
            "sample_rate": result.sample_rate,
        },
    )
    return {"id": row["id"], "cached": False, "duration": result.duration}


@register("synthesize")
def handle_synthesize(ctx, job) -> None:
    rows = store.read_jsonl(store.lines_path(ctx.settings, job.book_id, job.chapter_index))
    if not rows:
        raise RuntimeError(f"第 {job.chapter_index} 章没有行数据，请先跑 chapter_split")
    total = len(rows)
    done = 0
    failed = 0
    endpoint_down = False
    with ThreadPoolExecutor(max_workers=effective_concurrency(ctx)) as pool:
        futures = {pool.submit(synth_line, ctx, job, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - 单行失败不能拖垮整章
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
            finally:
                done += 1
                ctx.progress(job, done, total, row["id"])
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
