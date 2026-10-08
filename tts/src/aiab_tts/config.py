from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class TtsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIAB_TTS_", env_file=".env", extra="ignore")

    # qwen3 = Qwen3-TTS（VoiceDesign 设计音色 + Base 克隆，当前主线）
    # indextts = IndexTTS-2.5（保留可选，情绪向量通道）
    backend: str = "qwen3"
    host: str = "127.0.0.1"
    port: int = 8020
    data_dir: Path = Path("data")  # 参考音频缓存目录（相对 tts/）
    model_source: str = "local"  # modelscope | huggingface | local
    model_id: str = "IndexTeam/IndexTTS-2.5"
    model_dir: Path = Path("checkpoints")
    hf_endpoint: str = ""
    max_concurrency: int = 0  # 0 = 用后端推荐值
    device: str = "cuda:0"
    use_bf16: bool = True
    # ---- Qwen3-TTS（backend=qwen3）----
    # 两个 1.7B 模型分工：VoiceDesign 设计音色、Base 克隆全文
    qwen_base_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    qwen_design_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    # 注意力实现：留空 = 用 transformers 默认（Windows 上装不了 flash-attn）
    qwen_attn: str = ""
    # 8G 显存放不下两个 1.7B：默认同一时刻只驻留一个，另一个按需换入
    qwen_keep_both: bool = False
    # True 时额外加载 QwenEmotion（约 1.2GB 显存），用于「用一句话描述情绪」的 emoText 通道；
    # False 时只有 8 维 emoVector 通道可用
    use_qwen_emo: bool = False
    max_text_chars: int = 300
    # 批量合成：一次请求最多带几条（客户端会把同音色的连续几句打包过来）
    max_batch_items: int = 8
    queue_timeout_seconds: float = 600.0
    # 空闲归还显存：服务空闲这么久之后，把 PyTorch 缓存池里已经没人用的块还给驱动。
    # 只改"什么时候还"，不动任何在用张量；0 = 关闭。请求排队/推理中永远不会触发。
    idle_release_seconds: float = 20.0
    # 空闲块少于这个量就不值得归还：还回去下一批还得重新 cudaMalloc，得不偿失
    idle_release_min_free_mb: int = 256
    allow_download: bool = True
    verify_manifest: bool = True
    # ---- 速度旋钮（实测可查：/debug/tuning 能在不重载模型的情况下挨个试）----
    # GPT 采样束宽：上游默认 3（束搜索，解码开销 ×3）；1 = 纯采样，最快
    num_beams: int = 1
    # CFM（s2mel）迭代步数：上游把 25 写死在 infer_v2_5.py 里，这里改成可调
    diffusion_steps: int = 16
    # CFM 的 classifier-free guidance 强度（上游写死 0.7）
    inference_cfg_rate: float = 0.7


def get_settings(**overrides) -> TtsSettings:
    return TtsSettings(**overrides)
