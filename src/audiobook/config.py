import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 浏览器 UI 允许修改的设置白名单；data_dir 与密钥不在其中
OVERLAY_KEYS = (
    "engine",
    "tts_endpoints",
    "synth_concurrency",
    "synth_concurrency_max",
    "llm_base_url",
    "llm_model",
    "llm_temperature",
    "llm_concurrency",
    "ffmpeg_path",
    "export_target_sample_rate",
    "export_container",
    "export_mkv",
    "pause_scale",
    "pause_min_ms",
    "pause_max_ms",
    "pause_scene_extra_ms",
    "pause_tail_ms",
    "loudness_mode",
    "loudness_target_lufs",
    "loudness_true_peak",
    "loudness_rms_target_db",
)
SECRET_KEYS = ("llm_api_key",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AB_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    llm_concurrency: int = 8
    llm_base_url: str = "http://127.0.0.1:8009/v1"
    llm_api_key: str = "sk-local"
    llm_model: str = "deepseek-flash"
    llm_temperature: float = 0.6
    llm_timeout_seconds: float = 120.0
    llm_max_attempts: int = 2
    llm_max_output_tokens: int = 4096
    llm_json_mode: bool = True
    llm_chunk_chars: int = 8000
    llm_scene_window_chars: int = 8000
    synth_concurrency: int = 4
    engine: str = "fake"
    tts_timeout_seconds: float = 180.0
    tts_connect_timeout_seconds: float = 5.0
    tts_ref_upload_timeout_seconds: float = 120.0
    synth_concurrency_max: int = 16
    tts_health_cache_seconds: float = 5.0
    tts_breaker_seconds: float = 60.0
    tts_max_line_chunk_chars: int = 0
    tts_endpoints: list[str] = []
    worker_poll_seconds: float = 1.0
    lease_seconds: int = 30

    # ffmpeg（M3 产物导出）
    ffmpeg_path: str = ""
    ffmpeg_timeout_seconds: float = 1800.0
    # 导出格式
    export_target_sample_rate: int = 0  # 0 = 自动取各片段最高采样率
    export_container: str = "mkv"  # mkv | mp4
    export_mkv: bool = True  # 章节是否封装容器
    # 停顿（毫秒）
    pause_scale: float = 1.0
    pause_min_ms: int = 80
    pause_max_ms: int = 1200
    pause_scene_extra_ms: int = 500
    pause_tail_ms: int = 0  # 章节末尾额外静音
    # 响度：lufs | rms | off
    loudness_mode: str = "lufs"
    loudness_target_lufs: float = -16.0
    loudness_true_peak: float = -1.5
    loudness_rms_target_db: float = -20.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "service.db"

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"


def get_settings(**overrides) -> Settings:
    base = Settings()
    # overlay 必须从"最终生效的 data_dir"里读：显式传参优先，其次 .env/环境变量
    data_dir = Path(overrides.get("data_dir") or base.data_dir)
    overlay = load_overlay(base.model_copy(update={"data_dir": data_dir}))
    return Settings(**{**overlay, **overrides})


def load_overlay(settings) -> dict:
    """读 data/settings.json（UI 写的覆盖层）；文件缺失或损坏都返回空。"""
    path = Path(settings.data_dir) / "settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in OVERLAY_KEYS if key in data}


def save_overlay(settings, patch: dict) -> dict:
    unknown = sorted(set(patch) - set(OVERLAY_KEYS))
    if unknown:
        raise ValueError(f"不可通过界面修改的设置项：{', '.join(unknown)}")
    overlay = {**load_overlay(settings), **patch}
    path = Path(settings.data_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(overlay, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return overlay
