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

import logging
import os
import threading
from collections import OrderedDict
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


# ---------------------------------------------------------------------------
# 速度旋钮：GPT 束宽 / CFM 迭代步数（上游写死的地方从这里覆盖）
# ---------------------------------------------------------------------------


def make_conditioning_cache_per_voice(model, max_voices: int = 8) -> bool:
    """把上游"只有一个槽位"的参考音频条件缓存改成按音色缓存。

    上游 ``infer_generator`` 把参考音频的条件（spk_cond / style / prompt / mel / emo）
    存在实例属性上：换一个音色就整块作废，而且每次作废都 ``torch.cuda.empty_cache()``——
    一本多角色的书几乎每句都在换音色，于是每句都重算参考音频、还顺手清一次显存池。

    这里在模型外面按音色存一份，进入时装回实例属性（命中就直接走"没变过"的快路径）。
    装缓存到上游把这些值读进局部变量（``_set_gr_progress(0.1, "text processing...")``
    这一句）之间用锁护住，否则两个线程并发时可能把 A 的条件装给 B 用（串音色）。
    """
    if getattr(model, "_aiab_voice_cache_native", False):
        logger.info("参考音频条件缓存：上游已内置按音色缓存，跳过运行时补丁")
        return True
    original = getattr(model, "infer_generator", None)
    progress_hook = getattr(model, "_set_gr_progress", None)
    if original is None or progress_hook is None:
        logger.debug("参考音频条件缓存补丁跳过：模型上没有 infer_generator/_set_gr_progress")
        return False
    if getattr(original, "_aiab_voice_cache_wrapped", False):
        return True

    store: "OrderedDict[str, dict]" = OrderedDict()
    lock = threading.RLock()
    holder = threading.local()

    def capture(key: str) -> dict | None:
        tag = getattr(model, "cache_spk_audio_prompt", None)
        if tag is None or str(tag) != key or getattr(model, "cache_spk_cond", None) is None:
            return None
        entry = {
            "spk_cond": model.cache_spk_cond,
            "style": model.cache_s2mel_style,
            "prompt": model.cache_s2mel_prompt,
            "mel": model.cache_mel,
        }
        emo_key = getattr(model, "cache_emo_audio_prompt", None)
        if getattr(model, "cache_emo_cond", None) is not None and emo_key is not None and str(emo_key) == key:
            entry["emo_cond"] = model.cache_emo_cond
        return entry

    def install(entry: dict, key: str) -> None:
        model.cache_spk_cond = entry["spk_cond"]
        model.cache_s2mel_style = entry["style"]
        model.cache_s2mel_prompt = entry["prompt"]
        model.cache_mel = entry["mel"]
        model.cache_spk_audio_prompt = key
        if "emo_cond" in entry:
            model.cache_emo_cond = entry["emo_cond"]
            model.cache_emo_audio_prompt = key
        else:
            model.cache_emo_cond = None

    def clear() -> None:
        # 置空即"未命中"：上游不会再走 empty_cache 分支，显存池不会被反复清空
        model.cache_spk_cond = None
        model.cache_emo_cond = None

    def release_hold() -> None:
        held = getattr(holder, "lock", None)
        if held is not None:
            holder.lock = None
            held.release()

    original_progress = progress_hook

    def set_gr_progress(value, message="", *args, **kwargs):
        result = original_progress(value, message, *args, **kwargs)
        if message == "text processing..." and getattr(holder, "lock", None) is not None:
            key = getattr(holder, "key", None)
            entry = capture(key) if key else None
            if entry is not None:
                store[key] = entry
                store.move_to_end(key)
                while len(store) > max_voices:
                    store.popitem(last=False)
            release_hold()
        return result

    def wrapped(spk_audio_prompt, *args, **kwargs):
        key = str(spk_audio_prompt)
        lock.acquire()
        holder.lock = lock
        holder.key = key
        try:
            entry = store.get(key)
            if entry is not None:
                store.move_to_end(key)
                install(entry, key)
            else:
                clear()
            generator = original(spk_audio_prompt, *args, **kwargs)
        except BaseException:
            release_hold()
            raise

        def iterate():
            try:
                yield from generator
            finally:
                release_hold()

        return iterate()

    wrapped._aiab_voice_cache_wrapped = True
    model.infer_generator = wrapped
    model._set_gr_progress = set_gr_progress
    logger.info("参考音频条件缓存已改成按音色缓存（最多 %d 个音色，不会再清显存池）", max_voices)
    return True


# 运行时覆盖：/debug/tuning 改这里，不用重载模型就能对比参数
SPEED_TUNING: dict[str, float | int | None] = {
    "numBeams": None,        # None = 用 settings 里的值
    "diffusionSteps": None,
    "cfgRate": None,
}


def effective_tuning(settings) -> dict:
    """实际生效的三个旋钮（运行时覆盖优先于 .env/默认值）。"""
    beams = SPEED_TUNING["numBeams"]
    steps = SPEED_TUNING["diffusionSteps"]
    rate = SPEED_TUNING["cfgRate"]
    return {
        "numBeams": int(beams if beams is not None else settings.num_beams),
        "diffusionSteps": int(steps if steps is not None else settings.diffusion_steps),
        "cfgRate": float(rate if rate is not None else settings.inference_cfg_rate),
    }


def set_speed_tuning(patch: dict) -> dict:
    """只认这三个键；值给 None 表示恢复用配置默认。越界直接报错。"""
    limits = {"numBeams": (1, 16), "diffusionSteps": (4, 100), "cfgRate": (0.0, 2.0)}
    for key, (low, high) in limits.items():
        if key not in patch:
            continue
        value = patch[key]
        if value is None:
            SPEED_TUNING[key] = None
            continue
        number = float(value)
        if not low <= number <= high:
            raise ValueError(f"{key} 要在 {low}~{high} 之间，收到 {value}")
        SPEED_TUNING[key] = number if key == "cfgRate" else int(number)
    return dict(SPEED_TUNING)


def apply_cfm_speed(model, get_tuning) -> bool:
    """上游没打补丁时的兜底：把 CFM 的迭代步数 / CFG 强度变成可调参数。"""
    if upstream_supports_speed_patch():
        return True
    return _wrap_cfm_inference(model, get_tuning)


def upstream_supports_speed_patch() -> bool:
    """tts/index-tts 这份副本是否已内置"按音色条件缓存 + 可调 CFM 步数"。"""
    try:
        from indextts import infer_v2_5
    except Exception:  # noqa: BLE001 - 没装上游就当作没补丁
        return False
    return bool(getattr(infer_v2_5, "AIAB_PATCHED", False))


def upstream_supports_batch() -> bool:
    """装着的 index-tts 是否有批量解码（本仓库自带的副本有；换回官方 clone 就没有）。"""
    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception:  # noqa: BLE001 - 没装上游就当不支持
        return False
    return hasattr(IndexTTS2, "infer_batch")


def _wrap_cfm_inference(model, get_tuning) -> bool:
    """把 CFM 的迭代步数 / CFG 强度变成可调参数。

    上游 ``infer_v2_5.py`` 把 ``diffusion_steps = 25`` 和 ``inference_cfg_rate = 0.7``
    写死在函数体里，参数只能从 ``cfm.inference`` 这一层拦：签名是
    ``(mu, x_lens, prompt, style, f0, n_timesteps, temperature=1.0, inference_cfg_rate=0.5)``，
    所以第 6 个位置参数就是步数。
    """
    if getattr(model, "_aiab_cfm_speed_native", False):
        logger.info("CFM 调速：上游已内置可调步数，跳过运行时补丁")
        return True
    try:
        cfm = model.s2mel.models["cfm"]
    except Exception as exc:  # noqa: BLE001 - 上游结构变了就跳过，别拦住加载
        logger.debug("CFM 调速跳过：%s", exc)
        return False
    original = cfm.inference
    if getattr(original, "_aiab_speed_wrapped", False):
        return True

    def inference(*args, **kwargs):
        tuning = get_tuning()
        steps = tuning.get("diffusionSteps")
        if steps and len(args) > 5:
            args = list(args)
            args[5] = int(steps)
        rate = tuning.get("cfgRate")
        if rate is not None:
            kwargs["inference_cfg_rate"] = float(rate)
        return original(*args, **kwargs)

    inference._aiab_speed_wrapped = True
    cfm.inference = inference
    logger.info("CFM 调速已生效：%s", get_tuning())
    return True


# ---------------------------------------------------------------------------
# 内存自检：回答"权重到底在显存还是又回主机内存了"
# ---------------------------------------------------------------------------


def host_memory_report() -> dict:
    """当前进程的主机内存占用（MB）：常住工作集 / 提交 / 私有提交。"""
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes as wt

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return {}
        return {
            "workingSetMB": round(counters.WorkingSetSize / 1048576, 1),
            "commitMB": round(counters.PagefileUsage / 1048576, 1),
            "privateCommitMB": round(counters.PrivateUsage / 1048576, 1),
        }
    try:  # 非 Windows：/proc/self/status 够用
        with open("/proc/self/status", encoding="utf-8") as handle:
            info = {}
            for line in handle:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        return {
            "workingSetMB": round(int(info.get("VmRSS", "0 kB").split()[0]) / 1024, 1),
            "commitMB": round(int(info.get("VmSize", "0 kB").split()[0]) / 1024, 1),
        }
    except OSError:
        return {}


def model_memory_report(model) -> dict:
    """权重分布自检：每个大模块在什么设备、什么精度、多大；有没有张量留在主机内存。

    ``cpuResidentMB`` 就是"又加载到内存里"的那部分——正常情况下应该是 0。
    """
    import torch

    modules: list[dict] = []
    cpu_bytes = 0
    total_bytes = 0
    for name, attr in BF16_TARGETS.items():
        module = getattr(model, attr, None)
        if module is None or not hasattr(module, "parameters"):
            continue
        sizes = 0
        devices: list[str] = []
        dtypes: list[str] = []
        for tensor in list(module.parameters()) + list(module.buffers()):
            sizes += tensor.numel() * tensor.element_size()
            devices.append(str(tensor.device))
            dtypes.append(str(tensor.dtype).replace("torch.", ""))
        total_bytes += sizes
        if any(device.startswith("cpu") for device in devices):
            cpu_bytes += sizes
        modules.append(
            {
                "name": name,
                "attr": attr,
                "device": "+".join(sorted(set(devices))) or "?",
                "dtype": "+".join(sorted(set(dtypes))) or "?",
                "sizeMB": round(sizes / 1048576, 1),
            }
        )
    report = {
        "modules": modules,
        "totalMB": round(total_bytes / 1048576, 1),
        "cpuResidentMB": round(cpu_bytes / 1048576, 1),
    }
    if torch.cuda.is_available():
        report["cuda"] = {
            "allocatedMB": round(torch.cuda.memory_allocated() / 1048576, 1),
            "reservedMB": round(torch.cuda.memory_reserved() / 1048576, 1),
        }
    return report
