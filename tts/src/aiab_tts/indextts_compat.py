"""index-tts 上游的小补丁：ModelScope 仓库别名 + 并发安全。

index-tts 会把辅助模型的 HuggingFace repo id 直接拿去 ModelScope 查，但两边的命名
并不总是一样：BigVGAN 在 ModelScope 上属于 ``nv-community`` 而不是 ``nvidia``。
少了这条映射，每次启动都会先 404 一次、再退回 hf-mirror 兜底——国内这条路很慢，
449MB 的声码器能拖几个小时，看起来就像卡住了。

另一个补丁是推理并发：index-tts 的 GPT 推理模型把"当前请求的条件嵌入"存在实例属性
``cached_mel_emb`` 上，两个请求并发时会互相覆盖，直接 500。这里把它改成线程本地。

补丁写在我们的仓库里（``tts/index-tts`` 是 gitignore 的第三方目录，改那儿重装就丢）。
"""

from __future__ import annotations

import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# HuggingFace repo id -> ModelScope model id（逐个在 modelscope.cn 上核对过存在性）
MODELSCOPE_REPO_ALIASES: dict[str, str] = {
    "nvidia/bigvgan_v2_22khz_80band_256x": "nv-community/bigvgan_v2_22khz_80band_256x",
    "nvidia/bigvgan_v2_24khz_100band_256x": "nv-community/bigvgan_v2_24khz_100band_256x",
    "nvidia/bigvgan_v2_44khz_128band_256x": "nv-community/bigvgan_v2_44khz_128band_256x",
    "nvidia/bigvgan_v2_44khz_128band_512x": "nv-community/bigvgan_v2_44khz_128band_512x",
}


def modelscope_repo_id(repo_id: str) -> str:
    """把 HuggingFace 名字换成 ModelScope 上真存在的名字；没有别名就原样返回。"""
    return MODELSCOPE_REPO_ALIASES.get(repo_id, repo_id)


def apply_modelscope_aliases() -> list[str]:
    """把别名补进 index-tts 的 ``HF_TO_MODELSCOPE_REPO_MAP``，返回本次新补的 repo id。

    幂等：已经在表里的不再重复写。index-tts 没装、或上游以后换了映射表结构，就静默
    跳过——下载本身还有 hf-mirror 兜底，不该因为补丁失败而拦住服务启动。
    """
    try:
        from indextts.utils import model_download
    except Exception:  # noqa: BLE001 - 上游模块结构变了也不该炸
        logger.debug("没找到 indextts.utils.model_download，跳过 ModelScope 别名补丁")
        return []

    mapping = getattr(model_download, "HF_TO_MODELSCOPE_REPO_MAP", None)
    if not isinstance(mapping, dict):
        logger.debug("index-tts 的 HF_TO_MODELSCOPE_REPO_MAP 结构变了，跳过别名补丁")
        return []

    patched = [hf_id for hf_id, ms_id in MODELSCOPE_REPO_ALIASES.items() if mapping.get(hf_id) != ms_id]
    for hf_id in patched:
        mapping[hf_id] = MODELSCOPE_REPO_ALIASES[hf_id]
    if patched:
        logger.info("已补上 %d 条 ModelScope 仓库别名：%s", len(patched), "、".join(patched))
    return patched


def make_gpt_inference_thread_safe() -> bool:
    """把 GPT 推理模型的 ``cached_mel_emb`` 换成线程本地槽位，让多个 infer 能真并发。

    index-tts 每个请求开始时调用 ``inference_model.store_mel_emb(...)`` 把条件嵌入写进
    实例属性，``forward`` 里再读出来拼 ``inputs_embeds``。两个请求并发时，后写的会覆盖
    先写的：A 的 ``input_ids`` 配上 B 的条件嵌入，GPT2 forward 里
    ``inputs_embeds``（A 的文本 + B 的 mel 长度）与 ``position_embeds``（A 的 attention
    长度）对不上，直接抛

        RuntimeError: The size of tensor a (46) must match the size of tensor b (39)

    实测 3 路并发必炸。改成 threading.local 后每个推理线程各存各的槽位，
    GPT 这一段的共享状态就没了，可以真并发（模型权重本身是只读的）。

    幂等；上游结构变了就跳过并返回 False（调用方据此退回单并发）。
    """
    try:
        from indextts.gpt.model_v2 import GPT2InferenceModel
    except Exception:  # noqa: BLE001 - 没装 index-tts 或结构变了都不该拦住启动
        logger.debug("没找到 indextts.gpt.model_v2，跳过并发补丁")
        return False

    if getattr(GPT2InferenceModel, "_aiab_thread_local_mel_emb", False):
        return True
    if isinstance(getattr(GPT2InferenceModel, "cached_mel_emb", None), property):
        GPT2InferenceModel._aiab_thread_local_mel_emb = True
        return True

    import threading

    def _local(self):
        local = self.__dict__.get("_aiab_mel_emb_local")
        if local is None:
            local = threading.local()
            self.__dict__["_aiab_mel_emb_local"] = local
        return local

    def _get(self):
        return getattr(_local(self), "value", None)

    def _set(self, value):
        _local(self).value = value

    # nn.Module.__setattr__ 最终走 object.__setattr__，会正常触发这个 property 的 setter，
    # 所以 GPT2InferenceModel.__init__ 里的 self.cached_mel_emb = None 依然没问题。
    GPT2InferenceModel.cached_mel_emb = property(_get, _set)
    GPT2InferenceModel._aiab_thread_local_mel_emb = True
    logger.info("已给 IndexTTS 的 GPT 推理模型打上线程本地补丁（支持真并发）")
    return True


# ---------------------------------------------------------------------------
# 加载期优化：别在主机内存里再放一份权重
# ---------------------------------------------------------------------------

MMAP_MIN_BYTES = 16 * 1024 * 1024


@contextmanager
def _torch_load_mmap(min_bytes: int = MMAP_MIN_BYTES):
    """加载窗口内给大 checkpoint 的 ``torch.load`` 注入 ``mmap=True``。

    upstream 的 ``load_checkpoint`` / ``load_checkpoint2`` 都是
    ``torch.load(path, map_location="cpu")``：3.1GB 的 gpt.pth 会被完整读进主机内存，
    再 load_state_dict 拷进模型、再 .to(device) 拷进显存——主机里白白多一份。
    mmap 之后权重按页读、拷完即走，不再常驻。旧格式/异常自动回退成原来的读法。
    """
    import torch

    original = torch.load

    def patched(*args, **kwargs):
        target = args[0] if args else kwargs.get("f")
        size = 0
        if isinstance(target, (str, os.PathLike)):
            try:
                size = os.path.getsize(target)
            except OSError:
                size = 0
        map_location = kwargs.get("map_location")
        cpu_ok = map_location in (None, "cpu", "cpu:0") or getattr(map_location, "type", None) == "cpu"
        if size >= min_bytes and cpu_ok and "mmap" not in kwargs:
            try:
                return original(*args, **kwargs, mmap=True)
            except Exception as exc:  # noqa: BLE001 - 老格式/特殊文件回退正常读
                logger.debug("mmap 加载失败，回退普通读法：%s（%s）", target, exc)
        return original(*args, **kwargs)

    torch.load = patched
    try:
        yield
    finally:
        torch.load = original


def _wrap_init_on_device(cls, device: str) -> None:
    """让 ``cls.__init__`` 在目标设备上下文里跑：参数直接建在显存上。"""
    import torch

    original = cls.__init__
    if getattr(original, "_aiab_device_wrapped", False):
        return

    def wrapped(self, *args, **kwargs):
        with torch.device(device):
            original(self, *args, **kwargs)

    wrapped._aiab_device_wrapped = True
    wrapped._aiab_original = original
    cls.__init__ = wrapped


def _wrap_from_pretrained_on_device(cls, device: str) -> None:
    """HF ``from_pretrained``：用 device_map/low_cpu_mem_usage 直接进显存，失败自动回退。"""
    original = cls.from_pretrained
    if getattr(original, "_aiab_device_wrapped", False):
        return

    def wrapped(*args, **kwargs):
        fast = dict(kwargs)
        fast.setdefault("device_map", device)
        fast.setdefault("low_cpu_mem_usage", True)
        try:
            return original(*args, **fast)
        except Exception as exc:  # noqa: BLE001 - accelerate/device_map 不可用就退回原路
            logger.warning("直接加载到 %s 失败，回退 CPU 加载：%s", device, exc)
            return original(*args, **kwargs)

    wrapped._aiab_device_wrapped = True
    wrapped._aiab_original = original
    cls.from_pretrained = wrapped


@contextmanager
def fast_model_loading(device: str = "cuda"):
    """加载期优化：大 checkpoint mmap + 大模型直接在目标设备上构造。

    原来的加载路径是：CPU 随机初始化 → 读一份 checkpoint 到主机内存 → load_state_dict
    → .to(cuda) → 释放。主机里同时存在过两份权重（gpt.pth 就有 3.1GB），释放的页在
    Windows 上还常常不还给系统。这里把前两步都省掉：权重直接从文件映射进显存。

    ``AIAB_TTS_FAST_LOAD=0`` 可以关掉（出问题时的逃生开关）。
    """
    if os.environ.get("AIAB_TTS_FAST_LOAD", "1") not in ("1", "true", "True"):
        yield
        return

    import torch

    target = device if isinstance(device, str) else str(device)
    if target.startswith("cuda") and not torch.cuda.is_available():
        yield
        return

    try:
        from indextts.codec.models import EnhancedCodec
        from indextts.gpt.model_v2 import UnifiedVoice
        from indextts.s2mel.modules.campplus.DTDNN import CAMPPlus
        from indextts.s2mel.modules.commons import MyModel
    except Exception as exc:  # noqa: BLE001 - 上游结构变了就跳过优化，别拦住加载
        logger.debug("加载优化跳过（没找到目标模型类）：%s", exc)
        yield
        return

    # 补丁是幂等的、且故意不还原：同一进程后续再加载模型也走这条快路
    for cls in (UnifiedVoice, EnhancedCodec, MyModel, CAMPPlus):
        _wrap_init_on_device(cls, target)

    try:
        from transformers import Wav2Vec2BertModel

        _wrap_from_pretrained_on_device(Wav2Vec2BertModel, target)
    except Exception as exc:  # noqa: BLE001
        logger.debug("w2v-bert 直接加载补丁跳过：%s", exc)

    logger.info("加载优化已生效：mmap 读权重 + 模型直接在 %s 上构造", target)
    with _torch_load_mmap():
        yield


# ---------------------------------------------------------------------------
# bf16：把大模块降到半精度（默认只降 w2v-bert）
# ---------------------------------------------------------------------------

# 模块名 -> IndexTTS2 实例上的属性名
BF16_TARGETS = {
    "w2v": "semantic_model",  # 说话人/语义编码器（2.2GB→1.1GB，最值）
    "codec": "semantic_codec",  # 语义编解码（量化器）
    "s2mel": "s2mel",  # flow-matching + BigVGAN 容器
    "campplus": "campplus_model",  # 全局 style 编码
    "bigvgan": "bigvgan",  # 声码器（风险最高，默认不开）
}


def _cast_forward_inputs_to_param_dtype(module) -> None:
    """给模块的 forward 套一层：浮点输入自动转成权重 dtype，避免 fp32/bf16 混用报错。"""
    import torch

    dtype = next(module.parameters()).dtype
    original = module.forward

    def forward(*args, **kwargs):
        args = tuple(
            item.to(dtype) if torch.is_tensor(item) and item.is_floating_point() else item
            for item in args
        )
        kwargs = {
            key: (value.to(dtype) if torch.is_tensor(value) and value.is_floating_point() else value)
            for key, value in kwargs.items()
        }
        return original(*args, **kwargs)

    module.forward = forward


def apply_bf16_modules(model, use_bf16: bool, modules: str | None = None) -> list[str]:
    """把指定的大模块降到 bf16（权重 + 输入自动对齐 dtype）。

    默认只降 ``w2v``（w2v-bert 语义/说话人编码器，2.2GB→1.1GB，对音质最不敏感）。
    ``AIAB_TTS_BF16_MODULES`` 可以加别的：``w2v,codec,s2mel,campplus,bigvgan``——
    s2mel / bigvgan 降精度需要自己听一遍（flow-matching 的缓存 dtype、声码器数值都更敏感）。
    """
    import torch

    if not use_bf16:
        return []
    if not torch.cuda.is_available():
        logger.debug("没有 CUDA，跳过 bf16 模块转换")
        return []

    wanted = modules if modules is not None else os.environ.get("AIAB_TTS_BF16_MODULES", "w2v")
    applied: list[str] = []
    for name in [item.strip() for item in wanted.split(",") if item.strip()]:
        attr = BF16_TARGETS.get(name)
        if attr is None:
            logger.debug("bf16 目标不认识，跳过：%s", name)
            continue
        module = getattr(model, attr, None)
        if module is None:
            continue
        try:
            module.bfloat16()
            _cast_forward_inputs_to_param_dtype(module)
        except Exception as exc:  # noqa: BLE001 - 转换失败就保持 fp32，别拦住服务
            logger.warning("bf16 转换失败（%s），保持 fp32：%s", name, exc)
            continue
        applied.append(name)
    if applied:
        # 降精度时旧 fp32 权重已经没人引用，但还在 PyTorch 的缓存池里占着显存：
        # 归还给驱动，空闲显存才真的降下来
        torch.cuda.empty_cache()
        logger.info("bf16 已应用：%s（其余模块保持 fp32）", "、".join(applied))
    return applied
