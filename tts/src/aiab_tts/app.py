from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .config import TtsSettings
from .state import ServiceError, ServiceState


def build_backend(settings: TtsSettings):
    backend = (settings.backend or "qwen3").lower()
    if backend in ("qwen3", "qwen3-tts", "qwen"):
        from .backends.qwen3 import Qwen3TtsBackend  # 懒加载：只有真后端才 import torch 生态

        return Qwen3TtsBackend(settings)
    if backend in ("indextts", "indextts-2.5"):
        from .backends.indextts import IndexTtsBackend  # 懒加载：只有真后端才 import torch 生态

        return IndexTtsBackend(settings)
    raise ValueError(f"未知后端: {settings.backend}（支持 qwen3 / indextts）")


def build_state(settings: TtsSettings) -> ServiceState:
    return ServiceState(build_backend(settings), settings)


def create_app(settings: TtsSettings, state: ServiceState | None = None) -> FastAPI:
    app = FastAPI(title="AIAB TTS 服务")
    service = state or build_state(settings)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    def health():
        return service.health()

    @app.get("/capabilities")
    def capabilities():
        return service.capabilities()

    @app.get("/debug/memory")
    def debug_memory():
        """权重在显存还是内存：看 model.cpuResidentMB（正常是 0）与 host 快照。"""
        return service.memory_report()

    @app.get("/debug/tuning")
    def debug_tuning():
        """当前生效的速度旋钮（GPT 束宽 / CFM 步数 / CFG 强度）。"""
        return service.tuning()

    @app.post("/debug/tuning")
    def debug_tuning_update(payload: dict):
        """临时改旋钮、不重载模型：方便挑"多快还能听"的档位。传 null 恢复配置默认。"""
        try:
            return service.set_tuning(payload or {})
        except ValueError as exc:
            raise ServiceError("bad_request", str(exc), 400) from exc

    @app.post("/v1/refs")
    async def upload_ref(file: UploadFile = File(...), refText: str = Form("")):
        content = await file.read()
        if not content:
            raise ServiceError("bad_request", "参考音频为空", 400)
        return service.add_ref(content, refText)

    @app.post("/v1/synthesize")
    def synthesize(payload: dict):
        result = service.synthesize(payload)
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-Engine": result.engine,
                "X-Engine-Version": result.engine_version,
                "X-Duration-Sec": f"{result.duration_sec:.3f}",
                "X-Sample-Rate": str(result.sample_rate),
                "X-Elapsed-Ms": str(result.elapsed_ms),
            },
        )

    @app.post("/v1/synthesize_batch")
    def synthesize_batch(payload: dict):
        """同音色多条一次解码：返回 zip（000.wav… + manifest.json）。"""
        content, durations, elapsed_ms = service.synthesize_batch(payload)
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "X-Item-Count": str(len(durations)),
                "X-Item-Durations": ",".join(f"{value:.3f}" for value in durations),
                "X-Elapsed-Ms": str(elapsed_ms),
            },
        )

    @app.post("/v1/design")
    def design(payload: dict):
        """按音色描述造一段参考音频（Qwen3-TTS VoiceDesign）。

        每个角色调一次，产物由客户端落盘复用；返回体就是那一段 WAV。
        """
        result = service.design(payload)
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-Engine": result.engine,
                "X-Engine-Version": result.engine_version,
                "X-Duration-Sec": f"{result.duration_sec:.3f}",
                "X-Sample-Rate": str(result.sample_rate),
                "X-Elapsed-Ms": str(result.elapsed_ms),
            },
        )

    @app.post("/warmup")
    def warmup():
        return service.warmup()

    @app.post("/unload")
    def unload():
        return service.unload()

    return app
