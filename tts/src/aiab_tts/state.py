import io
import json
import logging
import tempfile
import threading
import time
import uuid
import wave
import zipfile
from pathlib import Path

from .backends.base import SynthesisRequest
from .schemas import BatchSynthPayload, SynthPayload

logger = logging.getLogger(__name__)


def wav_duration_seconds(path: Path) -> float:
    """读 WAV 头算时长；不是合法 WAV（例如测试里的假字节）返回 0.0 而不是抛错。"""
    try:
        with wave.open(str(path)) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except Exception:  # noqa: BLE001 - 参考音频合法性交给真实后端
        return 0.0


def gpu_info(settings) -> dict:
    info = {"device": settings.device, "vramTotalMB": None, "vramUsedMB": None}
    try:
        import torch
    except ImportError:
        return info
    if not torch.cuda.is_available():
        return info
    try:
        free, total = torch.cuda.mem_get_info()
        info["vramTotalMB"] = int(total / 1024 / 1024)
        info["vramUsedMB"] = int((total - free) / 1024 / 1024)
    except Exception:  # noqa: BLE001 - 拿不到显存不影响服务可用
        pass
    return info


def _release_gpu_memory() -> None:
    import gc

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


class ServiceError(Exception):
    """带错误码的服务异常，由 app 统一转成 {"detail": {"code", "message"}}。"""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ServiceState:
    def __init__(self, backend, settings):
        self.backend = backend
        self.settings = settings
        self.started_at = time.time()
        self.refs: dict[str, dict] = {}
        # 后端自报的是"安全上限"（模型不是线程安全的就只能是 1），配置只能往下调，不能往上顶
        backend_limit = max(1, int(backend.recommended_concurrency() or 1))
        requested = max(1, int(settings.max_concurrency or backend_limit))
        self.capacity = min(requested, backend_limit)
        if requested > backend_limit:
            logger.warning(
                "配置并发 %s 超过后端安全上限 %s，已按 %s 启动（后端模型不支持并发推理）",
                requested,
                backend_limit,
                self.capacity,
            )
        self._gate = threading.BoundedSemaphore(self.capacity)
        self.inflight = 0
        self.total_audio_sec = 0.0
        self.total_elapsed_ms = 0
        self._lock = threading.Lock()
        self._status = "unloaded"
        self.refs_dir = Path(settings.data_dir) / "refs"

    # --- 生命周期 ---

    def warmup(self) -> dict:
        started = time.monotonic()
        self._status = "loading"
        try:
            self.backend.load()
        except Exception:
            self._status = "error"
            raise
        self._status = "ok"
        return {
            "ok": True,
            "modelLoaded": self.backend.is_loaded(),
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }

    def unload(self) -> dict:
        started = time.monotonic()
        self.backend.unload()
        _release_gpu_memory()
        self._status = "unloaded"
        return {
            "ok": True,
            "modelLoaded": self.backend.is_loaded(),
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }

    def ensure_loaded(self) -> None:
        if not self.backend.is_loaded():
            self.warmup()

    def health(self) -> dict:
        average = (self.total_elapsed_ms / 1000.0) / self.total_audio_sec if self.total_audio_sec else 0.0
        return {
            "status": self._status if self.backend.is_loaded() else "unloaded",
            "modelLoaded": self.backend.is_loaded(),
            # 容量是"这台机器/这份配置能跑多少"，与是否已加载无关；
            # 报 0 会让客户端把冷启动中的服务当成故障，永远触发不了首次加载。
            "recommendedConcurrency": self.capacity,
            "inflight": self.inflight,
            "avgInferenceSecPerAudioSec": round(average, 3),
            "engine": self.backend.name,
            "engineVersion": self.backend.version,
            "modelSource": self.settings.model_source,
            "uptimeSec": int(time.time() - self.started_at),
            **gpu_info(self.settings),
        }

    def capabilities(self) -> dict:
        return self.backend.capabilities()

    def memory_report(self) -> dict:
        """内存/显存自检（模型在哪个设备、主机里有没有留副本）。"""
        reporter = getattr(self.backend, "memory_report", None)
        if reporter is None:
            return {"backend": self.backend.name, "loaded": self.backend.is_loaded()}
        return reporter()

    def tuning(self) -> dict:
        from .indextts_compat import effective_tuning

        return effective_tuning(self.settings)

    def set_tuning(self, patch: dict) -> dict:
        from .indextts_compat import effective_tuning, set_speed_tuning

        set_speed_tuning(patch or {})
        return effective_tuning(self.settings)

    # --- 参考音频 ---

    def add_ref(self, content: bytes, ref_text: str = "") -> dict:
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        ref_id = f"ref_{uuid.uuid4().hex[:12]}"
        path = self.refs_dir / f"{ref_id}.wav"
        path.write_bytes(content)
        self.refs[ref_id] = {"path": path, "refText": ref_text}
        return {
            "refId": ref_id,
            "durationSec": wav_duration_seconds(path),
            "sampleRate": int(self.backend.capabilities().get("sampleRate") or 22050),
        }

    # --- 合成 ---

    def synthesize(self, payload: dict):
        try:
            request_payload = SynthPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 校验失败统一走 bad_request
            raise ServiceError("bad_request", str(exc), 400) from exc

        ref = self.refs.get(request_payload.refId)
        if not ref:
            raise ServiceError("bad_ref", f"未知 refId: {request_payload.refId}", 404)
        max_chars = int(self.backend.capabilities().get("maxTextChars") or 300)
        if len(request_payload.text) > max_chars:
            raise ServiceError("bad_request", f"文本超过 maxTextChars({max_chars})，请在客户端分块", 400)
        if not self.backend.is_loaded():
            self.ensure_loaded()
        if not self._gate.acquire(timeout=self.settings.queue_timeout_seconds):
            raise ServiceError("busy", "服务繁忙，请退避重试", 503)
        with self._lock:
            self.inflight += 1
        try:
            request = SynthesisRequest(
                text=request_payload.text,
                ref_path=ref["path"],
                ref_text=ref.get("refText") or "",
                lang=request_payload.lang,
                emo_vector=tuple(request_payload.emoVector) if request_payload.emoVector else None,
                emotion_text=request_payload.emoText or "",
                rate=request_payload.rate,
                pronunciation=request_payload.pronunciation,
                seed=request_payload.seed,
            )
            result = self.backend.synthesize(request)
        except ServiceError:
            raise
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                logger.exception("TTS 引擎显存不足")
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            # 引擎 500 以前只有一句 message，堆栈被吞掉；出问题根本没法定位，这里必须留痕
            logger.exception("TTS 引擎推理失败")
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
        with self._lock:
            self.total_audio_sec += result.duration_sec
            self.total_elapsed_ms += result.elapsed_ms
        return result

    # --- 批量合成（同一个音色，一次解码多条）---

    def synthesize_batch(self, payload: dict) -> tuple[bytes, list[float], int]:
        """把同一个音色的 N 条文本一次解码，返回 (zip 字节, 每条时长, 耗时ms)。

        zip 里是 ``000.wav``… 与 ``manifest.json``；调用方按序号取。批量请求在并发门里
        只占 1 个位置（显存占用由服务端自己控制），所以客户端可以放心把 4~8 条打成一包。
        """
        try:
            request_payload = BatchSynthPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 校验失败统一走 bad_request
            raise ServiceError("bad_request", str(exc), 400) from exc

        ref = self.refs.get(request_payload.refId)
        if not ref:
            raise ServiceError("bad_ref", f"未知 refId: {request_payload.refId}", 404)
        items = request_payload.items
        limit = max(1, int(self.settings.max_batch_items))
        if len(items) > limit:
            raise ServiceError("bad_request", f"一次最多 {limit} 条（服务端 AIAB_TTS_MAX_BATCH_ITEMS）", 400)
        max_chars = int(self.backend.capabilities().get("maxTextChars") or 300)
        for item in items:
            if len(item.text) > max_chars:
                raise ServiceError("bad_request", f"文本超过 maxTextChars({max_chars})，请在客户端分块", 400)
        if not self.backend.is_loaded():
            self.ensure_loaded()
        if not self._gate.acquire(timeout=self.settings.queue_timeout_seconds):
            raise ServiceError("busy", "服务繁忙，请退避重试", 503)
        with self._lock:
            self.inflight += 1
        started = time.monotonic()
        try:
            requests = [
                SynthesisRequest(
                    text=item.text,
                    ref_path=ref["path"],
                    ref_text=ref.get("refText") or "",
                    lang=request_payload.lang,
                    emo_vector=tuple(item.emoVector) if item.emoVector else None,
                    emotion_text=item.emoText or "",
                    rate=item.rate,
                    pronunciation=item.pronunciation,
                    seed=item.seed,
                )
                for item in items
            ]
            batch_fn = getattr(self.backend, "synthesize_batch", None)
            # emoText 走的是 QwenEmotion 通道，批量路径没实现：退回逐条（结果一致，只是慢一点）
            use_batch = batch_fn is not None and not any(item.emoText for item in items)
            with tempfile.TemporaryDirectory() as tmp_dir:
                out_paths = [Path(tmp_dir) / f"{index:03d}.wav" for index in range(len(requests))]
                if use_batch:
                    durations = [float(value) for value in batch_fn(requests, out_paths)]
                else:
                    durations = []
                    for index, request in enumerate(requests):
                        result = self.backend.synthesize(request)
                        out_paths[index].write_bytes(result.audio)
                        durations.append(float(result.duration_sec))
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                    for index, path in enumerate(out_paths):
                        archive.write(path, f"{index:03d}.wav")
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "count": len(durations),
                                "durations": [round(value, 3) for value in durations],
                                "batched": bool(use_batch),
                            },
                            ensure_ascii=False,
                        ),
                    )
                content = buffer.getvalue()
        except ServiceError:
            raise
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                logger.exception("TTS 批量合成显存不足")
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            logger.exception("TTS 批量合成失败")
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self.total_audio_sec += sum(durations)
            self.total_elapsed_ms += elapsed_ms
        return content, durations, elapsed_ms
