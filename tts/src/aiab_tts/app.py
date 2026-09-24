from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .config import TtsSettings
from .state import ServiceError, ServiceState


def build_backend(settings: TtsSettings):
    backend = (settings.backend or "indextts").lower()
    if backend in ("indextts", "indextts-2.5"):
        from .backends.indextts import IndexTtsBackend  # 懒加载：只有真后端才 import torch 生态

        return IndexTtsBackend(settings)
    raise ValueError(f"未知后端: {settings.backend}（只支持 indextts）")


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

    @app.post("/warmup")
    def warmup():
        return service.warmup()

    @app.post("/unload")
    def unload():
        return service.unload()

    return app
