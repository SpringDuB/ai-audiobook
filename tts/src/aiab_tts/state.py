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

from .backends.base import SynthesisRequest, SynthesisResult
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


def _empty_cuda_cache() -> None:
    """把 PyTorch 缓存池里没人用的块还给驱动（绝不碰在用张量）。"""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _release_gpu_memory() -> None:
    import gc

    gc.collect()
    _empty_cuda_cache()


def _cuda_pool_mb() -> tuple[float, float] | None:
    """(allocated, reserved) 单位 MB；没装 torch / 没 CUDA 返回 None。

    ``reserved - allocated`` 就是"张量已经 free 掉、但缓存池还攥着没还给驱动"的量。
    """
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    try:
        allocated = float(torch.cuda.memory_allocated()) / 1048576
        reserved = float(torch.cuda.memory_reserved()) / 1048576
    except Exception:  # noqa: BLE001 - 统计拿不到不该影响服务
        return None
    return allocated, reserved


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
        # 空闲归还显存：_last_activity 任何请求进来（含还在闸门外排队的）都会刷新
        self._last_activity = time.monotonic()
        self._last_idle_release = 0.0
        self.idle_released_mb = 0.0
        self._start_idle_reclaimer()

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

    # --- 空闲归还显存（只还"缓存池里的空闲块"，不动在用张量、不卸载模型）---

    def _touch(self) -> None:
        """有请求进来就重置空闲计时。排在闸门外的请求也算，避免它们在等的时候池子被清。"""
        with self._lock:
            self._last_activity = time.monotonic()

    def _start_idle_reclaimer(self) -> None:
        self._idle_reclaimer: threading.Thread | None = None
        seconds = float(getattr(self.settings, "idle_release_seconds", 0) or 0)
        if seconds <= 0:
            logger.info("空闲归还显存：已关闭（AIAB_TTS_IDLE_RELEASE_SECONDS=0）")
            return
        interval = max(1.0, min(5.0, seconds / 4.0))
        thread = threading.Thread(
            target=self._idle_reclaim_loop,
            args=(interval,),
            name="tts-idle-release",
            daemon=True,
        )
        thread.start()
        self._idle_reclaimer = thread
        logger.info(
            "空闲归还显存：已开启（空闲 %.0fs 且空闲块 ≥ %dMB 时 empty_cache，每 %.1fs 检查一次）",
            seconds,
            int(getattr(self.settings, "idle_release_min_free_mb", 256) or 0),
            interval,
        )

    def _idle_reclaim_loop(self, interval: float) -> None:
        while True:
            time.sleep(interval)
            try:
                self._maybe_release_idle_memory()
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次失败就退出
                logger.exception("空闲归还显存失败")

    def _maybe_release_idle_memory(self) -> float:
        """空闲够久且有值得归还的空闲块时，把它们还给驱动；返回归还的 MB 数。

        三条硬性前置条件：模型已加载、没有在飞请求、最近 ``idle_release_seconds``
        内没有任何请求进来。少一条就直接跳过——请求进行中清池子会把别的请求的
        空闲块一起还掉，逼它们重新走慢速 cudaMalloc。
        """
        seconds = float(getattr(self.settings, "idle_release_seconds", 0) or 0)
        if seconds <= 0 or not self.backend.is_loaded():
            return 0.0
        now = time.monotonic()
        with self._lock:
            idle_for = now - self._last_activity
            busy = self.inflight > 0
            since_release = now - self._last_idle_release
        if busy or idle_for < seconds or since_release < seconds:
            return 0.0

        before = _cuda_pool_mb()
        if before is None:
            return 0.0
        threshold = float(getattr(self.settings, "idle_release_min_free_mb", 0) or 0)
        if before[1] - before[0] < threshold:
            return 0.0

        _empty_cuda_cache()
        after = _cuda_pool_mb() or before
        freed = max(0.0, before[1] - after[1])
        with self._lock:
            self._last_idle_release = time.monotonic()
            self.idle_released_mb += freed
        logger.info(
            "空闲归还显存：reserved %.0fMB → %.0fMB（归还 %.0fMB，allocated %.0fMB）",
            before[1],
            after[1],
            freed,
            after[0],
        )
        return freed

    def health(self) -> dict:
        average = (self.total_elapsed_ms / 1000.0) / self.total_audio_sec if self.total_audio_sec else 0.0
        return {
            "status": self._status if self.backend.is_loaded() else "unloaded",
            "modelLoaded": self.backend.is_loaded(),
            # 容量是"这台机器/这份配置能跑多少"，与是否已加载无关；
            # 报 0 会让客户端把冷启动中的服务当成故障，永远触发不了首次加载。
            "recommendedConcurrency": self.capacity,
            "inflight": self.inflight,
            "idleReleasedMB": round(self.idle_released_mb, 1),
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
        self._touch()  # 请求一进门就重置空闲计时（含后面闸门外排队的）
        try:
            request_payload = SynthPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 校验失败统一走 bad_request
            raise ServiceError("bad_request", str(exc), 400) from exc

        ref = None
        if request_payload.refId:
            ref = self.refs.get(request_payload.refId)
            if not ref:
                raise ServiceError("bad_ref", f"未知 refId: {request_payload.refId}", 404)
        elif not (request_payload.voicePrompt or "").strip():
            # 两条通道至少给一条：refId（克隆）或 voicePrompt（按描述生成）
            raise ServiceError("bad_request", "缺少 refId（克隆）或 voicePrompt（按描述生成）", 400)
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
                ref_path=ref["path"] if ref else None,
                ref_text=(ref.get("refText") or "") if ref else "",
                lang=request_payload.lang,
                voice_prompt=request_payload.voicePrompt or "",
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
            self._touch()
        with self._lock:
            self.total_audio_sec += result.duration_sec
            self.total_elapsed_ms += result.elapsed_ms
        return result

    # --- 音色设计（Qwen3-TTS VoiceDesign：描述 → 一段参考音频）---

    def design(self, payload: dict):
        """按自然语言描述造一段参考音频（Qwen3-TTS VoiceDesign）。

        每个角色一次：产物落盘后整本书都拿它做克隆参考，所以这里慢一点没关系
        （首次要加载 VoiceDesign 权重）。返回 SynthesisResult 形状的结果。
        """
        self._touch()
        caps = self.backend.capabilities()
        if not caps.get("voiceDesign"):
            raise ServiceError(
                "bad_request",
                f"当前 TTS 后端 {self.backend.name} 不支持音色设计（需要 backend=qwen3）",
                400,
            )
        text = str((payload or {}).get("text") or "").strip()
        instruct = str((payload or {}).get("instruct") or "").strip()
        lang = str((payload or {}).get("lang") or "ZH").strip() or "ZH"
        if not text:
            raise ServiceError("bad_request", "缺少 text：设计参考音频要说的话", 400)
        if not instruct:
            raise ServiceError("bad_request", "缺少 instruct：音色的自然语言描述", 400)
        max_chars = int(caps.get("maxTextChars") or 300)
        if len(text) > max_chars:
            raise ServiceError("bad_request", f"text 超过 maxTextChars({max_chars})", 400)
        if not self._gate.acquire(timeout=self.settings.queue_timeout_seconds):
            raise ServiceError("busy", "服务繁忙，请退避重试", 503)
        with self._lock:
            self.inflight += 1
        started = time.monotonic()
        try:
            design_fn = getattr(self.backend, "design", None)
            if design_fn is None:
                raise ServiceError("bad_request", "后端没有实现 design()", 400)
            audio, sample_rate, _spoken = design_fn(text=text, instruct=instruct, lang=lang)
        except ServiceError:
            raise
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                logger.exception("音色设计显存不足")
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            logger.exception("音色设计失败")
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
            self._touch()
        duration = 0.0
        try:
            with wave.open(io.BytesIO(audio)) as handle:
                duration = handle.getnframes() / float(handle.getframerate())
        except Exception:  # noqa: BLE001 - 时长只用于日志/响应头
            pass
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self.total_audio_sec += duration
            self.total_elapsed_ms += elapsed_ms
        return SynthesisResult(
            audio=audio,
            duration_sec=duration,
            sample_rate=int(sample_rate),
            engine=self.backend.name,
            engine_version=self.backend.version,
            elapsed_ms=elapsed_ms,
        )

    # --- 批量合成（同一个音色，一次解码多条）---

    def synthesize_batch(self, payload: dict) -> tuple[bytes, list[float], int]:
        """把同一个音色的 N 条文本一次解码，返回 (zip 字节, 每条时长, 耗时ms)。

        zip 里是 ``000.wav``… 与 ``manifest.json``；调用方按序号取。批量请求在并发门里
        只占 1 个位置（显存占用由服务端自己控制），所以客户端可以放心把 4~8 条打成一包。
        """
        self._touch()  # 请求一进门就重置空闲计时（含后面闸门外排队的）
        try:
            request_payload = BatchSynthPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 校验失败统一走 bad_request
            raise ServiceError("bad_request", str(exc), 400) from exc

        items = request_payload.items
        ref = None
        if request_payload.refId:
            ref = self.refs.get(request_payload.refId)
            if not ref:
                raise ServiceError("bad_ref", f"未知 refId: {request_payload.refId}", 404)
        elif not any((item.voicePrompt or "").strip() for item in items):
            # 两条通道至少给一条：refId（克隆）或每条自己的 voicePrompt（按描述生成）
            raise ServiceError(
                "bad_request", "缺少 refId（克隆）或 item.voicePrompt（按描述生成）", 400
            )
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
                    ref_path=ref["path"] if ref else None,
                    ref_text=(ref.get("refText") or "") if ref else "",
                    lang=request_payload.lang,
                    voice_prompt=item.voicePrompt or "",
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
            self._touch()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self.total_audio_sec += sum(durations)
            self.total_elapsed_ms += elapsed_ms
        return content, durations, elapsed_ms
