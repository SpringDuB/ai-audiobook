import json
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# 浏览器 UI 允许修改的设置白名单；data_dir 与密钥不在其中
OVERLAY_KEYS = (
    "engine",
    "emotion_mode",
    "tts_endpoints",
    "synth_concurrency",
    "synth_concurrency_max",
    "tts_backend",
    "tts_model_source",
    "tts_model_dir",
    "tts_hf_endpoint",
    "tts_port",
    "llm_base_url",
    "llm_model",
    "llm_temperature",
    "llm_concurrency",
    "ffmpeg_path",
    "export_target_sample_rate",
    "export_container",
    "export_mkv",
    "loudness_mode",
    "loudness_target_lufs",
    "loudness_true_peak",
    "loudness_rms_target_db",
)
SECRET_KEYS = ("llm_api_key",)

# 文本描述情绪通道（QwenEmotion）：实现完整保留，但**暂时不开放**。
# 重新开放只改这一处：默认值回 "text"，设置页的下拉会自动出现，tts start 会按设置加载 QwenEmotion。
EMOTION_TEXT_ENABLED = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AB_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    llm_concurrency: int = 8
    llm_base_url: str = "http://127.0.0.1:8009/v1"
    llm_api_key: str = "sk-local"
    llm_model: str = "deepseek-flash"
    llm_temperature: float = 0.6
    # 推理型模型（deepseek-flash 这类会先输出 reasoning_content）先"想"后写，
    # 思考 tokens 也算在 max_tokens 里：给不够就会出现"满预算但正文为空"。
    llm_timeout_seconds: float = 600.0
    llm_max_attempts: int = 2
    llm_max_output_tokens: int = 64000
    llm_json_mode: bool = True
    # 流式请求：非流式长请求会被网关按空闲超时掐断（Server disconnected），别关
    llm_stream: bool = True
    llm_chunk_chars: int = 8000
    # 逐句情感标注的窗口上限：一章切成若干窗口分别请求 LLM。
    # 一句一条记录，窗口太大输出会被 llm_max_output_tokens 截断，所以字符数与句数都要卡
    llm_line_window_chars: int = 1600
    llm_line_window_sentences: int = 80
    synth_concurrency: int = 4
    # 同音色一次解码几条（批量合成）：1 = 关掉；服务端不支持批量时自动退回 1
    synth_batch_size: int = 4
    # 同时跑几个批量请求（一个批量请求内部已经是 N 条并行，别再叠 GPU 压力）
    synth_batch_workers: int = 1
    engine: str = "http"  # 只有 http：合成必须走独立 TTS 服务
    tts_timeout_seconds: float = 180.0
    tts_connect_timeout_seconds: float = 5.0
    tts_ref_upload_timeout_seconds: float = 120.0
    # 音色设计（Qwen3-TTS VoiceDesign）：首次要把 Base 换成 VoiceDesign 权重再生成，
    # 慢是正常的；超时给足，别让一个角色设计到一半就被判失败
    tts_design_timeout_seconds: float = 900.0
    synth_concurrency_max: int = 16
    # 情绪控制通道：vector（8 维向量，当前唯一开放的通道）| text（文本描述，暂时关闭）
    emotion_mode: Literal["text", "vector"] = "vector"
    tts_health_cache_seconds: float = 5.0
    tts_breaker_seconds: float = 60.0
    tts_max_line_chunk_chars: int = 0
    tts_endpoints: list[str] = []
    worker_poll_seconds: float = 1.0
    # worker 一次能同时跑几个任务：不同书之间可以并行；同一本书的任务按队列顺序串行
    # （分析 → 选角 → 合成 → 渲染 → 合本 之间有文件依赖，并行会互相踩）
    worker_concurrency: int = 2
    # 同时跑几个"吃 TTS/GPU"的合成任务：TTS 服务端本来就有并发闸门，
    # 多个合成任务并排只会互相抢显存，默认 1
    worker_tts_jobs: int = 1
    lease_seconds: int = 30

    # 一键启动的本机 TTS 服务（tts/ 子项目）的启动参数
    # qwen3 = Qwen3-TTS（VoiceDesign 设计音色 + Base 克隆，当前主线）
    # indextts = IndexTTS-2.5（情绪向量通道，保留可选）
    tts_backend: str = "qwen3"
    tts_model_source: str = "local"  # modelscope | huggingface | local
    tts_model_dir: str = "checkpoints"
    tts_hf_endpoint: str = ""
    tts_port: int = 8020

    # ffmpeg（M3 产物导出）
    ffmpeg_path: str = ""
    ffmpeg_timeout_seconds: float = 1800.0
    # 导出格式
    export_target_sample_rate: int = 0  # 0 = 自动取各片段最高采样率
    export_container: str = "mkv"  # mkv | mp4
    export_mkv: bool = True  # 章节是否封装容器
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
    settings = Settings(**{**overlay, **overrides})
    if not EMOTION_TEXT_ENABLED and settings.emotion_mode != "vector":
        # 通道关闭期间，不管是谁写进来的 text（.env / settings.json / 旧前端）一律回落向量
        settings = settings.model_copy(update={"emotion_mode": "vector"})
    return settings


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
