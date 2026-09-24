from .errors import TtsUnavailable
from .pool import TtsPool

HTTP_ENGINE_NAMES = {"http", "tts", "indextts", "indextts-2.5"}


def build_engine(settings):
    """按配置装配合成引擎：只有 http（连独立 TTS 服务）一种。"""
    name = (settings.engine or "http").strip().lower()
    if name in HTTP_ENGINE_NAMES:
        if not settings.tts_endpoints:
            raise TtsUnavailable(
                "未配置 TTS 端点：到设置页点「一键启动 TTS 服务」，或设置 AB_TTS_ENDPOINTS（例如 http://127.0.0.1:8020）"
            )
        return TtsPool(settings.tts_endpoints, settings)
    if name == "fake":
        raise ValueError("fake 引擎已移除：合成只走独立 TTS 服务，请到设置页一键启动")
    raise ValueError(f"未知引擎: {settings.engine}")
