import gc
import io
import json
import logging
import math
import re
import tempfile
import threading
import time
import uuid
import wave
import zipfile
from dataclasses import replace
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
    """把 PyTorch 缓存池里没人用的块还给驱动（绝不碰在用张量）。

    有线程正在捕获 CUDA Graph 时必须让路：捕获窗口里任何一次 cudaFree
    （empty_cache 就是干这个的）都会让捕获方报
    "CUDA error: operation not permitted when stream is capturing"。
    捕获只发生在某个 batch 形状第一次出现时，让这一次归还错过窗口没有任何代价。
    """
    try:
        import torch
    except ImportError:
        return
    if not torch.cuda.is_available():
        return
    try:
        from .backends.qwen3_fast import capture_active
    except Exception:  # noqa: BLE001 - 不是 qwen3 后端 / 模块缺失：照常归还
        capture_active = None
    if capture_active is not None and capture_active():
        logger.debug("跳过 empty_cache：有线程正在捕获 CUDA Graph")
        return
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


_OOM_MARKERS = ("out of memory", "outofmemoryerror", "cuda_error_out_of_memory")


def is_oom_error(exc: BaseException) -> bool:
    """显存不足判定：torch 的 OutOfMemoryError、驱动抛的 AcceleratorError、老式 RuntimeError 都要认出来。

    认不出来的话请求会直接变成 500，客户端拿不到 ``code=oom``，只能把端点整个熔断
    （上一轮故障链就是「清理阶段 OOM → 500 → 客户端熔断 60s → 整批任务连锁阵亡」）。
    """
    if type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError"):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _OOM_MARKERS)


_SENTENCE_BOUNDARY = re.compile(r"[^。！？!?；;…\.\n]+[。！？!?；;…\.\n]*")


def split_text_for_retry(text: str, max_parts: int = 2) -> list[str]:
    """把一条长台词切成最多 ``max_parts`` 段，供「单条一包也 OOM」时降级。

    优先按句末标点均衡切；整句没有标点就按长度硬切（尽量落在逗号/空格上，
    别把词切开）。切不动时原样返回一段，调用方据此放弃切分。
    """
    source = (text or "").strip()
    if len(source) < 2:
        return [text or ""]
    pieces = [piece.strip() for piece in _SENTENCE_BOUNDARY.findall(source) if piece.strip()]
    if len(pieces) < 2:
        middle = len(source) // 2
        window_start = max(0, middle - 8)
        window = source[window_start : middle + 8]
        offset = max((window.rfind(mark) for mark in "，,、 "), default=-1)
        cut = window_start + offset + 1 if offset >= 0 else middle
        head, tail = source[:cut].strip(), source[cut:].strip()
        return [head, tail] if head and tail else [source]
    total = sum(len(piece) for piece in pieces)
    target = max(1, math.ceil(total / max_parts))
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for piece in pieces:
        if current and current_len + len(piece) > target and len(groups) < max_parts - 1:
            groups.append(current)
            current, current_len = [], 0
        current.append(piece)
        current_len += len(piece)
    if current:
        groups.append(current)
    return ["".join(group) for group in groups] or [source]


def _concat_wav(chunks: list[bytes]) -> tuple[bytes, float]:
    """把同一模型产出的多段 WAV 字节首尾拼成一段，返回 (字节, 时长秒)。"""
    buffer = io.BytesIO()
    params = None
    total_frames = 0
    with wave.open(buffer, "wb") as out:
        for chunk in chunks:
            with wave.open(io.BytesIO(chunk), "rb") as handle:
                current = handle.getparams()
                if params is None:
                    params = current
                    out.setparams(current)
                elif (
                    current.nchannels,
                    current.sampwidth,
                    current.framerate,
                ) != (params.nchannels, params.sampwidth, params.framerate):
                    raise ValueError("分段音频参数不一致，无法拼接")
                out.writeframes(handle.readframes(handle.getnframes()))
                total_frames += handle.getnframes()
    duration = total_frames / float(params.framerate) if params else 0.0
    return buffer.getvalue(), duration


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
        self._last_pool_release = 0.0
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
                # 并发跑着的时候也要盯着池子：多路批量背靠背时请求边界永远"不空闲"，
                # 池子会在几分钟内虚胖到 9~10GB（物理 8.2GB，溢出到共享显存，越跑越慢）
                self.release_after_request()
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

    def release_after_request(self) -> float:
        """请求边界归还：把池子里攒着没人用的块还给驱动。

        长任务里请求是背靠背来的（并发跑时更是永远有请求在飞），"空闲 20 秒"和
        "没有任何在飞请求"这两个窗口都等不到，PyTorch 缓存池就会一直停在历史峰值，
        实测能堆到 9.5GB（物理只有 8.2GB，多出来的部分已经溢出到共享显存，速度还慢）。

        两条放行条件：
          - 没有请求在飞：空闲块 ≥ after_request_release_min_free_mb 就还；
          - 还有请求在飞：空闲块 ≥ release_under_load_min_free_mb（默认 1.5GB）也还 ——
            这时池子明显是"虚胖"，还给驱动不会动任何在用张量。
        再加一个最小间隔（默认 3s）防止抖动。
        """
        if not bool(getattr(self.settings, "release_after_request", True)):
            return 0.0
        if not self.backend.is_loaded():
            return 0.0
        now = time.monotonic()
        with self._lock:
            busy = self.inflight > 0
            since_last = now - self._last_pool_release
        cooldown = float(getattr(self.settings, "release_min_interval_seconds", 3.0) or 0)
        if since_last < cooldown:
            return 0.0
        before = _cuda_pool_mb()
        if before is None:
            return 0.0
        free = before[1] - before[0]
        threshold = float(
            (
                getattr(self.settings, "release_under_load_min_free_mb", 1536)
                if busy
                else getattr(self.settings, "after_request_release_min_free_mb", 256)
            )
            or 0
        )
        if free < threshold:
            # 池子没有虚胖：这次不还（也把冷却时间推开，别每个请求都去读一次显存）
            with self._lock:
                self._last_pool_release = now
                return 0.0
        with self._lock:
            self._last_pool_release = now
        _empty_cuda_cache()
        after = _cuda_pool_mb() or before
        freed = max(0.0, before[1] - after[1])
        if freed > 0:
            with self._lock:
                self.idle_released_mb += freed
            logger.info(
                "请求边界归还显存：reserved %.0fMB → %.0fMB（归还 %.0fMB，allocated %.0fMB，%s）",
                before[1],
                after[1],
                freed,
                after[0],
                "并发中" if busy else "空闲",
            )
        return freed

    # --- 请求内自愈（OOM：只留模型权重，其余显存全还掉后降档重试）---

    def _oom_retry_budget(self) -> int:
        return max(0, int(getattr(self.settings, "oom_max_retries", 3) or 0))

    def _safe_release_after_request(self, *, stage: str) -> None:
        """请求边界的显存清理：清理失败只记日志，绝不能顶掉本次请求的结果。

        实录：一次 500 就是这里 ``empty_cache`` 抛 CUDA OOM，异常穿透 ``finally``
        顶掉了正常响应，客户端拿不到 ``code=oom`` 只能把端点熔断 60s。
        """
        for attempt in (1, 2):
            try:
                self.release_after_request()
                return
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响本次请求成败
                if attempt == 2:
                    logger.warning(
                        "请求边界归还显存失败（已忽略，%s，不影响本次请求）：%s", stage, exc
                    )
                else:
                    time.sleep(0.05)

    def _clear_cuda_graphs(self) -> int:
        """丢掉后端捕获过的 CUDA Graph：图池是常驻显存，``empty_cache`` 还不了它。"""
        clear = getattr(self.backend, "clear_cuda_graphs", None)
        if not callable(clear):
            return 0
        try:
            return int(clear() or 0)
        except Exception:  # noqa: BLE001 - 清图失败不该挡住重试
            logger.warning("OOM 自愈：清理 CUDA Graph 失败（忽略）", exc_info=True)
            return 0

    def _recover_after_oom(self, *, stage: str, holder: BaseException | None = None) -> None:
        """OOM 自愈：模型权重留着，把其余占用的显存尽量还给驱动。

        顺序是有讲究的：
          1. 断开异常对象持有的 traceback —— 它会钉住当次推理的激活值/KV cache，
             不松手的话后面 ``empty_cache`` 也还不了这些块（这是"越跑越涨"的元凶之一）；
          2. 清 CUDA Graph 图池（每张图自带静态缓冲，常驻且不归 empty_cache 管）；
          3. ``gc.collect()`` 收掉没有引用的张量；
          4. ``empty_cache()`` 把缓存池里的空闲块还给驱动。
        """
        if holder is not None and getattr(holder, "__traceback__", None) is not None:
            try:
                holder.__traceback__ = None  # 松手后激活值才会真的被 free
            except Exception:  # noqa: BLE001 - 个别异常对象不可写，忽略
                pass
        before = _cuda_pool_mb()
        graphs = self._clear_cuda_graphs()
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                try:
                    torch.cuda.synchronize()
                except Exception:  # noqa: BLE001 - 上下文可能已经脏了，继续 empty_cache
                    logger.debug("OOM 自愈：cuda synchronize 失败（继续）", exc_info=True)
        except ImportError:
            pass
        try:
            _empty_cuda_cache()
        except Exception as exc:  # noqa: BLE001 - 还不动就算了，降档重试本身还有机会成功
            logger.warning("OOM 自愈：empty_cache 失败（继续重试）：%s", exc)
        wait = float(getattr(self.settings, "oom_retry_wait_seconds", 0.2) or 0)
        if wait > 0:
            time.sleep(min(wait, 2.0))
        after = _cuda_pool_mb()
        freed = max(0.0, before[1] - after[1]) if before and after else 0.0
        logger.warning(
            "OOM 自愈：清掉 %s 张 CUDA Graph、归还 %.0fMB 空闲块（allocated %.0f→%.0fMB，%s）",
            graphs,
            freed,
            before[0] if before else -1.0,
            after[0] if after else -1.0,
            stage,
        )

    def _synthesize_with_heal(self, request: SynthesisRequest, depth: int = 0) -> SynthesisResult:
        """单条合成：OOM 时先还显存再重试；仍不行就按句切分，各段跑完拼回一条。"""
        budget = self._oom_retry_budget()
        attempt = 0
        while True:
            try:
                return self.backend.synthesize(request)
            except Exception as exc:  # noqa: BLE001 - 非 OOM 原样抛出，交给调用方分类
                if not is_oom_error(exc) or attempt >= budget:
                    raise
                attempt += 1
                self._recover_after_oom(
                    stage=f"单条第 {attempt}/{budget} 次重试：{request.text[:16]}",
                    holder=exc,
                )
                if depth >= 4:
                    continue
                pieces = split_text_for_retry(request.text)
                if len(pieces) < 2:
                    continue
                logger.warning("OOM 自愈：单条切成 %d 段重试（%s）", len(pieces), request.text[:16])
                results = [
                    self._synthesize_with_heal(replace(request, text=piece), depth + 1)
                    for piece in pieces
                ]
                audio, duration = _concat_wav([item.audio for item in results])
                first = results[0]
                return SynthesisResult(
                    audio=audio,
                    duration_sec=duration,
                    sample_rate=first.sample_rate,
                    engine=first.engine,
                    engine_version=first.engine_version,
                    elapsed_ms=sum(item.elapsed_ms for item in results),
                )

    def _run_chunk(self, requests, out_paths, chunk: list[int], batch_fn) -> list[float]:
        selected = [requests[index] for index in chunk]
        paths = [out_paths[index] for index in chunk]
        if batch_fn is not None and len(selected) > 1:
            return [float(value) for value in batch_fn(selected, paths)]
        values: list[float] = []
        for request, path in zip(selected, paths):
            result = self.backend.synthesize(request)
            Path(path).write_bytes(result.audio)
            values.append(float(result.duration_sec))
        return values

    def _synthesize_chunks(self, requests, out_paths, batch_fn) -> list[float]:
        """批量合成的 OOM 自愈循环（整包 → 减半 → … → 单条 → 按句切分）。

        自愈只作用于本次请求内部：任何一次成功之后立刻回到客户端要的整包大小，
        不做跨请求的降档记忆（成功后直接升回满档）。
        """
        durations = [0.0] * len(requests)
        budget = self._oom_retry_budget()
        full_pack = max(1, len(requests))
        pack = full_pack
        retries = 0
        index = 0
        while index < len(requests):
            chunk = list(range(index, min(index + pack, len(requests))))
            try:
                values = self._run_chunk(requests, out_paths, chunk, batch_fn)
            except Exception as exc:  # noqa: BLE001 - 非 OOM 原样抛出
                if not is_oom_error(exc) or retries >= budget:
                    raise
                retries += 1
                self._recover_after_oom(
                    stage=f"{len(chunk)} 条一包，第 {retries}/{budget} 次重试",
                    holder=exc,
                )
                if pack > 1:
                    pack = max(1, pack // 2)
                    logger.warning("OOM 自愈：%d 条整包失败 → 降到 %d 条重试", full_pack, pack)
                    continue
                values = self._retry_single_split(requests, out_paths, chunk[0])
            for position, value in zip(chunk, values):
                durations[position] = value
            index += len(chunk)
            retries = 0
            if pack != full_pack:  # 成功后直接升回整包
                logger.info("OOM 自愈：自第 %d 条起恢复 %d 条整包", index, full_pack)
                pack = full_pack
        return durations

    def _retry_single_split(self, requests, out_paths, position: int) -> list[float]:
        """单条一包也 OOM：把这条台词按句切小分别合成，再把音频拼回一条。"""
        request = requests[position]
        pieces = split_text_for_retry(request.text)
        if len(pieces) < 2:
            raise ServiceError("oom", f"显存不足且台词无法再切分：{request.text[:40]}", 503)
        logger.warning("OOM 自愈：单条仍失败 → 按句切成 %d 段重试", len(pieces))
        audio_chunks: list[bytes] = []
        for piece in pieces:
            # 每段仍走同一套自愈：段还是太长（继续 OOM）就再往下切，最多 4 层
            result = self._synthesize_with_heal(replace(request, text=piece), depth=1)
            audio_chunks.append(result.audio)
        merged, duration = _concat_wav(audio_chunks)
        Path(out_paths[position]).write_bytes(merged)
        return [duration]

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
        report = (
            {"backend": self.backend.name, "loaded": self.backend.is_loaded()}
            if reporter is None
            else reporter()
        )
        pool = _cuda_pool_mb()
        if pool is not None:
            allocated, reserved = pool
            report["cudaPool"] = {
                "allocatedMB": round(allocated, 1),
                "reservedMB": round(reserved, 1),
                "freeBlocksMB": round(max(0.0, reserved - allocated), 1),
            }
        report["idleReleasedMB"] = round(self.idle_released_mb, 1)
        return report

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
            result = self._synthesize_with_heal(request)
        except ServiceError:
            raise
        except RuntimeError as exc:
            if is_oom_error(exc):
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
            self._safe_release_after_request(stage="单条合成")
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
            try:
                audio, sample_rate, _spoken = design_fn(text=text, instruct=instruct, lang=lang)
            except Exception as exc:  # noqa: BLE001 - 只有 OOM 才自愈重试
                if not is_oom_error(exc) or self._oom_retry_budget() <= 0:
                    raise
                logger.warning("音色设计 OOM：清显存后重试一次")
                self._recover_after_oom(stage="音色设计重试", holder=exc)
                audio, sample_rate, _spoken = design_fn(text=text, instruct=instruct, lang=lang)
        except ServiceError:
            raise
        except RuntimeError as exc:
            if is_oom_error(exc):
                logger.exception("音色设计显存不足")
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            logger.exception("音色设计失败")
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
            self._touch()
            self._safe_release_after_request(stage="音色设计")
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
                durations = self._synthesize_chunks(
                    requests, out_paths, batch_fn if use_batch else None
                )
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
            if is_oom_error(exc):
                logger.exception("TTS 批量合成显存不足")
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            logger.exception("TTS 批量合成失败")
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
            self._touch()
            self._safe_release_after_request(stage="批量合成")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self.total_audio_sec += sum(durations)
            self.total_elapsed_ms += elapsed_ms
        return content, durations, elapsed_ms
