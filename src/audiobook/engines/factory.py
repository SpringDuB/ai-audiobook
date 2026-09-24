from .errors import TtsUnavailable
from .fake import FakeEngine
from .pool import TtsPool

HTTP_ENGINE_NAMES = {"http", "tts", "indextts", "indextts-2.5"}


def build_engine(settings):
    """按配置装配合成引擎：fake（无 GPU 验证）或 http（连独立 TTS 服务）。"""
    name = (settings.engine or "fake").strip().lower()
    if name == "fake":
        return FakeEngine()
    if name in HTTP_ENGINE_NAMES:
        if not settings.tts_endpoints:
            raise TtsUnavailable(
                "未配置 TTS 端点：请设置 AB_TTS_ENDPOINTS（例如 http://127.0.0.1:8020）并确认 TTS 服务已启动"
            )
        return TtsPool(settings.tts_endpoints, settings)
    raise ValueError(f"未知引擎: {settings.engine}")
