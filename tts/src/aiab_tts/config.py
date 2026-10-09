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
    # code_predictor 每条 codebook 都要跑一次 HF generate（一帧音频 = 15 次），
    # 每帧 1.7 万次微内核、GPU 利用率只有 30~45%。这里换成等价快路径：
    #   graph 手工 CUDA Graph（实测 1.75x，逐样本比特一致）—— 默认
    #   loop  手写采样循环（去掉 HF 簿记，约 1.06x）
    #   pad   定长前缀无 cache（形状恒定，可编译）
    #   off   上游实现
    fast_predictor: str = "graph"
    # codec 解码分块：`speech_tokenizer.decode` 会把整包一起上采样，8 条长旁白时
    # 单步峰值就能到 7GB（物理只有 8.2GB）→ 溢出共享显存，整包从 25s 变 46s。
    # 实测（8 条 74~94 字旁白）：不分块 169.8 ms/帧、峰值 allocated 7.6GB/reserved 12.2GB；
    # 每次解 2 条 → 88.4 ms/帧、峰值 5.0GB/7.3GB，且波形与不分块逐样本等长。
    # 0 = 不分块。
    decode_chunk: int = 2
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
    # 请求边界归还：一个请求跑完、当前没有任何在飞请求时，如果池子里攒着这么多空闲块就还给驱动。
    # 长任务里请求是背靠背来的，"空闲 20 秒"永远等不到 —— 没有这一条，缓存池会一直停在历史峰值
    # （实测：空闲 5.2GB → 连跑几十批后 7.9GB 不回落）。False = 关闭，只保留空闲归还。
    release_after_request: bool = True
    after_request_release_min_free_mb: int = 256
    # 并发跑着的时候也会攒空闲块：多路批量背靠背时"没有在飞请求"的窗口根本不存在。
    # 空闲块堆到这个量就照样还给驱动（正在用的张量不受影响，代价只是别的请求重新 cudaMalloc）。
    # 实测：2 路批量跑着，allocated 4.5GB、reserved 9.5GB（物理 8.2GB，已溢出到共享显存）。
    release_under_load_min_free_mb: int = 1536
    # 两次归还之间的最小间隔：避免每跑完一个请求就 empty_cache 造成抖动
    release_min_interval_seconds: float = 3.0
    # ---- OOM 自愈（请求内）----
    # 一次请求里显存不足时：断开异常持有的激活值引用 → 清 CUDA Graph 图池 → gc → empty_cache，
    # 然后把批量包减半重试（8→4→2→1），单条仍不行就按句切分再拼回。
    # 自愈只作用于本次请求：一旦成功立刻回到整包，不做跨请求的降档记忆。
    oom_max_retries: int = 3
    # 每次自愈重试前的短暂等待：给驱动真正回收显存留一点时间
    oom_retry_wait_seconds: float = 0.2
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
