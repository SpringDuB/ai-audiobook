"""Talker 的预分配 KV cache：干掉 DynamicCache 每步 `torch.cat` 的 O(n²) 拷贝。

实测（第 3 章真实长旁白，pack 8）：

    最长行 13.0s（163 帧）→ 130 ms/帧
    最长行 19.4s（242 帧）→ 181 ms/帧
    短行     ~7.5s（ 94 帧）→  75 ms/帧

每帧成本随已生成长度线性上升，是"KV cache 每步重建"的典型特征：
`DynamicLayer.update` 每层每步都 `torch.cat([old, new])`，缓存多大就拷多少，
整段生成累计 O(n²) 字节搬运。换成预分配 + 按位置写之后是 O(n)。

对外行为与 DynamicLayer 完全一致（`get_mask_sizes` 同样返回"已写入 + 本步"），
所以贪婪解码下结果应当逐样本比特一致 —— 由 `data/tmp-debug` 的对拍脚本验证。
"""

from __future__ import annotations

import logging

import torch
from transformers.cache_utils import Cache, CacheLayerMixin

logger = logging.getLogger(__name__)


class PreallocatedLayer(CacheLayerMixin):
    """一个层的 K/V 缓冲：按 `length` 位置写入，满了就按 growth 扩容（摊还 O(n)）。"""

    is_sliding = False

    def __init__(self, capacity: int, growth: float = 1.5):
        super().__init__()
        self.capacity = max(64, int(capacity))
        self.growth = max(1.1, float(growth))
        self.length = 0
        self.batch = self.heads = self.head_dim = 0
        self.dtype = None
        self.device = None

    # --- 生命周期 ---

    def lazy_initialization(self, key_states: torch.Tensor) -> None:
        self.dtype = key_states.dtype
        self.device = key_states.device
        self.batch, self.heads, _, self.head_dim = key_states.shape
        self.length = 0
        self.keys = torch.zeros(
            (self.batch, self.heads, self.capacity, self.head_dim),
            dtype=self.dtype,
            device=self.device,
        )
        self.values = torch.zeros_like(self.keys)
        self.is_initialized = True

    def _grow(self, needed: int) -> None:
        capacity = max(int(needed), int(self.capacity * self.growth))
        keys = torch.zeros(
            (self.batch, self.heads, capacity, self.head_dim),
            dtype=self.dtype,
            device=self.device,
        )
        values = torch.zeros_like(keys)
        if self.length:
            keys[:, :, : self.length] = self.keys[:, :, : self.length]
            values[:, :, : self.length] = self.values[:, :, : self.length]
        self.keys, self.values, self.capacity = keys, values, capacity
        logger.debug("KV cache 扩容到 %d 帧（已写入 %d）", capacity, self.length)

    # --- 接口 ---

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        cache_kwargs: dict | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if not self.is_initialized or key_states.shape[0] != self.batch:
            self.lazy_initialization(key_states)
        incoming = int(key_states.shape[-2])
        if self.length + incoming > self.capacity:
            self._grow(self.length + incoming)
        start = self.length
        end = start + incoming
        self.keys[:, :, start:end] = key_states
        self.values[:, :, start:end] = value_states
        self.length = end
        # 只把"已写入"的前缀交给注意力：与 DynamicCache 的语义一致
        return self.keys[:, :, :end], self.values[:, :, :end]

    def get_mask_sizes(self, cache_position: torch.Tensor) -> tuple[int, int]:
        return self.length + int(cache_position.shape[0]), 0

    def get_seq_length(self) -> int:
        return self.length

    def get_max_cache_shape(self) -> int:
        return self.capacity

    def reset(self) -> None:
        self.length = 0


def build_preallocated_cache(capacity: int, growth: float = 1.5) -> Cache:
    """给一次 generate 造一个预分配 cache（每层按同样的容量分配）。"""
    return Cache(layer_class_to_replicate=lambda: PreallocatedLayer(capacity, growth))


def install_preallocated_cache(model, growth: float = 1.5) -> str:
    """把 talker.generate 包一层：注入预分配 cache。

    model：Qwen3TTSForConditionalGeneration

    只挂实例属性（不动类），所以卸载干净、也不影响别的实例。
    容量 = 提示词长度 + 预估帧数；预估来自 input_ids 的 token 数
    （中文约 1 token/字，实测 2.8 帧/字，这里按 3.0 估），估少了会按 growth 扩容。
    """
    talker = model.talker
    if getattr(model, "_aiab_prealloc_installed", False):
        return "预分配 KV cache 已安装（跳过重复安装）"

    original_model_generate = model.generate
    original_talker_generate = talker.generate

    def model_generate(*args, **kwargs):
        input_ids = kwargs.get("input_ids")
        counts: list[int] = []
        if isinstance(input_ids, (list, tuple)):
            for item in input_ids:
                try:
                    counts.append(int(item.shape[-1]))
                except Exception:  # noqa: BLE001 - 估不准就用默认容量
                    continue
        talker._aiab_frames_hint = max(256, int(max(counts, default=0) * 3.0))
        return original_model_generate(*args, **kwargs)

    def talker_generate(*args, **kwargs):
        if kwargs.get("past_key_values") is None:
            inputs_embeds = kwargs.get("inputs_embeds")
            if inputs_embeds is not None:
                hint = int(getattr(talker, "_aiab_frames_hint", 256))
                capacity = int(inputs_embeds.shape[1]) + hint
                kwargs["past_key_values"] = build_preallocated_cache(capacity, growth)
        return original_talker_generate(*args, **kwargs)

    model.generate = model_generate
    talker.generate = talker_generate
    model._aiab_prealloc_installed = True
    model._aiab_original_generate = original_model_generate
    talker._aiab_original_generate = original_talker_generate
    return f"Talker KV cache → 预分配（增长系数 {growth}）"


def uninstall_preallocated_cache(model) -> None:
    talker = model.talker
    original = getattr(model, "_aiab_original_generate", None)
    if original is not None:
        model.generate = original
        talker.generate = talker._aiab_original_generate
        model._aiab_prealloc_installed = False


def _longest_frames(encoded: list) -> int:
    """列表里最长的一条有多少帧（12Hz codec：audio_codes 形状 (T, Q)）。"""
    longest = 0
    for item in encoded:
        codes = item.get("audio_codes") if isinstance(item, dict) else None
        if codes is None:
            continue
        try:
            longest = max(longest, int(codes.shape[0]))
        except Exception:  # noqa: BLE001 - 形状怪就当作未知
            continue
    return longest


def install_chunked_decode(model, chunk: int = 2, threshold_frames: int = 150) -> str:
    """把 codec 解码切块：峰值显存按 chunk 线性下降。

    实测（8 条长旁白）：talker 逐步只涨 ~1.8MB/步，但整包峰值 7.0GB —— 大头在
    `speech_tokenizer.decode`：8 条 × 228 帧 × 1920 倍上采样，末级张量单个就近 450MB。
    解码结果是逐条独立的，切块后拼接完全等价（对拍 max|Δ| = 0）。

    短包整包解码更快（8 条 × 87 帧：7.11x vs 分块后 6.76x），所以只在"最长的一条
    超过 threshold_frames"时才分块 —— 这样长短两种情况都拿到最好成绩。
    """
    tokenizer = model.speech_tokenizer
    if getattr(tokenizer, "_aiab_chunk_installed", False):
        return f"codec 解码已分块（chunk={tokenizer._aiab_chunk_size}）"
    original = tokenizer.decode

    def decode_chunked(encoded, *args, **kwargs):
        if not isinstance(encoded, list) or len(encoded) <= 1:
            return original(encoded, *args, **kwargs)
        size = len(encoded)
        if _longest_frames(encoded) >= int(threshold_frames):
            size = max(1, int(chunk))
        if size >= len(encoded):
            return original(encoded, *args, **kwargs)
        wavs: list = []
        rate = None
        for start in range(0, len(encoded), size):
            part, rate = original(encoded[start : start + size], *args, **kwargs)
            wavs.extend(part)
        return wavs, rate

    tokenizer.decode = decode_chunked
    tokenizer._aiab_chunk_installed = True
    tokenizer._aiab_original_decode = original
    tokenizer._aiab_chunk_size = chunk
    tokenizer._aiab_chunk_threshold = threshold_frames
    return f"codec 解码分块：最长 ≥{threshold_frames} 帧时每次解 {chunk} 条，否则整包"


def uninstall_chunked_decode(model) -> None:
    tokenizer = model.speech_tokenizer
    original = getattr(tokenizer, "_aiab_original_decode", None)
    if original is not None:
        tokenizer.decode = original
        tokenizer._aiab_chunk_installed = False
