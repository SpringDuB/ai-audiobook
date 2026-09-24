import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import jobs as jobs_mod
from .. import store
from ..cache import cache_key, params_from_line
from ..worker import register

logger = logging.getLogger(__name__)


def resolve_voice_id(settings, book_id: str, speaker: str) -> str:
    casting = store.read_json(store.book_dir(settings, book_id) / "voices" / "casting.json", default={})
    entry = casting.get(speaker) if isinstance(casting, dict) else None
    if isinstance(entry, dict):
        return entry.get("voice_id") or "default"
    return "default"


def _synth_one(ctx, job, row: dict) -> dict:
    caps = ctx.engine.capabilities()
    voice_id = resolve_voice_id(ctx.settings, job.book_id, row["speaker"])
    params = params_from_line(row, caps)
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
    issues: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, ctx.settings.synth_concurrency)) as pool:
        futures = {pool.submit(_synth_one, ctx, job, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - 单行失败不能拖垮整章
                logger.warning("行 %s 合成失败: %s", row["id"], exc)
                issues.append(
                    {
                        "id": row["id"],
                        "chapter": job.chapter_index,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "text": row["text"],
                    }
                )
            finally:
                done += 1
                ctx.progress(job, done, total, row["id"])
    for issue in issues:
        store.append_jsonl(store.issues_path(ctx.settings, job.book_id), issue)
    if len(issues) == total:
        raise RuntimeError("整章所有行都合成失败")
    ctx.progress(job, total, total, f"完成，失败 {len(issues)} 行")
    jobs_mod.enqueue(ctx.conn, "post", job.book_id, job.chapter_index)
