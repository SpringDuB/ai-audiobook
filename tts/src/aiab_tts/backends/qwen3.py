"""Qwen3-TTS 后端：按"音色描述"直接生成（VoiceDesign），参考音频克隆是备选路径。

主用法（有声书）：
  每一句的合成参数里带一段 **音色描述**（= 角色基础音色描述 + 本句语气描述），
  后端调 VoiceDesign 的 generate_voice_design(text, language, instruct) 生成这一句。
  这样每条台词都按自己的语气演，不受参考音频的语气"传染"。

备选用法（沿用参考音频）：
  给了参考音频（库存音色 / 手工上传）就走 Base 的克隆通道，和以前一样。

显存：1.7B 的 VoiceDesign 常驻约 4.0GB（实测 RTX 4060 8G）；
Base 只有真的用到克隆时才加载，两个模型同时驻留会挤爆 8G 卡，默认互斥换入。
权重放 tts/checkpoints/Qwen3-TTS-12Hz-1.7B-{Base,VoiceDesign}，
装法见 scripts/install_qwen3.ps1。
"""

import io
import logging
import threading
import time
import wave
from pathlib import Path

from .base import SynthesisRequest, SynthesisResult

logger = logging.getLogger(__name__)

# 主应用的分析链用语言代码（ZH/EN/JP），Qwen3-TTS 要的是语言名
LANGUAGE_NAMES = {
    "ZH": "Chinese",
    "CN": "Chinese",
    "EN": "English",
    "JP": "Japanese",
    "JA": "Japanese",
    "KR": "Korean",
    "KO": "Korean",
    "DE": "German",
    "FR": "French",
    "RU": "Russian",
    "PT": "Portuguese",
    "ES": "Spanish",
    "IT": "Italian",
}

DEFAULT_SAMPLE_RATE = 24000
# 描述为空时兜底：VoiceDesign 允许空 instruct，但什么都别说容易"漂"，给个中性描述
FALLBACK_INSTRUCT = "自然清晰的声音，语速适中，语气平稳。"
# 尾部静音裁剪：批量解码时同一包是同步跑的，先说完的句子会陪跑到最长那条结束，
# 中间那些帧解码出来是**数字静音**（实测：5 个字的一句生成 16.3s，其中 15s 是静音）。
# 只裁尾巴、保留自然收束；头部不动（怕吃掉起音的气口）。
TAIL_SILENCE_KEEP_SECONDS = 0.35
TAIL_SILENCE_RATIO = 0.01          # 相对峰值的 -40dB
TAIL_SILENCE_MIN_TRIM_SECONDS = 0.5  # 尾巴不到半秒就别动它


def _trim_tail(waveform, sample_rate: int):
    """裁掉波形尾部的静音，返回（可能变短的）波形；不是数值数组时原样返回。"""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - 没 numpy 的部署不存在
        return waveform
    data = np.asarray(waveform)
    if data.size == 0:
        return waveform
    mono = np.abs(data if data.ndim == 1 else data.reshape(len(data), -1).max(axis=1))
    peak = float(mono.max())
    if peak <= 0:
        return waveform
    loud = np.nonzero(mono >= peak * TAIL_SILENCE_RATIO)[0]
    if loud.size == 0:
        return waveform
    keep = int(int(sample_rate) * TAIL_SILENCE_KEEP_SECONDS)
    end = int(loud[-1]) + keep + 1
    min_trim = int(int(sample_rate) * TAIL_SILENCE_MIN_TRIM_SECONDS)
    if end >= len(mono) or (len(mono) - end) < min_trim:
        return waveform
    return data[:end]


def language_name(code: str | None) -> str:
    """语言代码 → Qwen3-TTS 认识的语言名；认不出就交给模型自动判断。"""
    text = (code or "").strip()
    if not text:
        return "Auto"
    known = LANGUAGE_NAMES.get(text.upper())
    if known:
        return known
    # 已经是语言全名（Chinese/English…）就直接用；短代码认不出就交给模型自己判断
    return text if len(text) > 3 else "Auto"


def _wav_bytes(waveform, sample_rate: int) -> bytes:
    """numpy 波形 → WAV 字节（后端只依赖 soundfile，不引额外编码器）。"""
    import soundfile as sf

    waveform = _trim_tail(waveform, sample_rate)
    buffer = io.BytesIO()
    sf.write(buffer, waveform, int(sample_rate), format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _wav_seconds(payload: bytes) -> float:
    try:
        with wave.open(io.BytesIO(payload)) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except Exception:  # noqa: BLE001 - 时长只用于响应头，读不出来也不该失败
        return 0.0


class Qwen3TtsBackend:
    name = "qwen3-tts"
    version = "1.7b"
    DEFAULT_CONCURRENCY = 3

    def __init__(self, settings):
        self.settings = settings
        self._models: dict[str, object] = {}
        # 模型换入换出必须串行：两个线程同时 load 会把显存挤爆
        self._switch_lock = threading.Lock()
        # clone prompt 缓存：key = (模型, 参考音频路径, mtime_ns, 参考文本)
        self._prompts: dict[tuple, object] = {}
        self._cache_lock = threading.Lock()

    # ---------------------------------------------------------------- 模型

    @property
    def _base_id(self) -> str:
        return str(getattr(self.settings, "qwen_base_id", "") or "Qwen/Qwen3-TTS-12Hz-1.7B-Base")

    @property
    def _design_id(self) -> str:
        return str(
            getattr(self.settings, "qwen_design_id", "") or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
        )

    def _local_dir(self, model_id: str) -> Path:
        return Path(self.settings.model_dir) / model_id.split("/")[-1]

    def _model_path(self, model_id: str) -> str:
        """本地目录有就用本地目录；model_source=local 且缺权重时给出可执行的提示。"""
        local = self._local_dir(model_id)
        if (local / "config.json").exists():
            return str(local)
        source = (self.settings.model_source or "local").lower()
        if source == "local":
            raise RuntimeError(
                f"缺少 {model_id} 权重（找的是 {local}）。"
                "先在 tts/ 目录执行：powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1"
            )
        if not self.settings.allow_download:
            raise RuntimeError(f"缺少 {model_id} 权重，且已关闭自动下载（allow_download=False）")
        self._download(model_id, local, source)
        return str(local)

    @staticmethod
    def _download(model_id: str, target: Path, source: str) -> None:
        target.mkdir(parents=True, exist_ok=True)
        logger.info("下载模型 %s → %s（source=%s）", model_id, target, source)
        if source == "modelscope":
            from modelscope import snapshot_download

            snapshot_download(model_id, local_dir=str(target))
            return
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=model_id, local_dir=str(target))

    def _unload_kind(self, kind: str) -> None:
        model = self._models.pop(kind, None)
        if model is None:
            return
        del model
        self._drop_prompts()
        from ..state import _release_gpu_memory

        _release_gpu_memory()
        logger.info("已卸载 %s 模型并归还显存", kind)

    def _load_kind(self, kind: str):
        import torch
        from qwen_tts import Qwen3TTSModel

        model_id = self._base_id if kind == "base" else self._design_id
        path = self._model_path(model_id)
        dtype = torch.bfloat16 if self.settings.use_bf16 else torch.float32
        kwargs: dict = {"device_map": self.settings.device, "dtype": dtype}
        attn = str(getattr(self.settings, "qwen_attn", "") or "").strip()
        if attn:
            kwargs["attn_implementation"] = attn
        started = time.monotonic()
        logger.info("加载 Qwen3-TTS %s：%s（dtype=%s）", kind, path, dtype)
        model = Qwen3TTSModel.from_pretrained(path, **kwargs)
        logger.info("Qwen3-TTS %s 加载完成，用时 %.1fs", kind, time.monotonic() - started)
        mode = str(getattr(self.settings, "fast_predictor", "") or "").strip().lower()
        if mode not in ("", "off", "none", "0", "false"):
            try:
                from .qwen3_fast import install_fast_predictor

                logger.info(install_fast_predictor(model.model, mode=mode))
            except Exception:  # noqa: BLE001 - 加速失败必须退回原路径，不能拖垮服务
                logger.exception("启用 code_predictor 快路径失败，继续用上游实现")
        chunk = int(getattr(self.settings, "decode_chunk", 0) or 0)
        if chunk > 0:
            try:
                from .qwen3_cache import install_chunked_decode

                logger.info(install_chunked_decode(model.model, chunk=chunk))
            except Exception:  # noqa: BLE001
                logger.exception("codec 解码分块失败，继续用原始解码")
        return model

    def _model(self, kind: str):
        """取模型；同一时刻只驻留一个（除非显式允许同时保留）。"""
        with self._switch_lock:
            model = self._models.get(kind)
            if model is not None:
                return model
            if not bool(getattr(self.settings, "qwen_keep_both", False)):
                for other in [name for name in self._models if name != kind]:
                    self._unload_kind(other)
            model = self._load_kind(kind)
            self._models[kind] = model
            return model

    def load(self) -> None:
        """预热 = 加载 VoiceDesign（主路径就是它）。"""
        self._model("design")

    def unload(self) -> None:
        with self._switch_lock:
            for kind in list(self._models):
                self._unload_kind(kind)
        self._drop_prompts()

    def is_loaded(self) -> bool:
        return bool(self._models)

    # ---------------------------------------------------------------- 参考提示（克隆通道）

    def _prompt(self, model, ref_path: Path, ref_text: str):
        """clone prompt 按（参考音频 + 参考文本）缓存：整本书一个音色只算一次。"""
        try:
            stamp = ref_path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        key = (id(model), str(ref_path), stamp, (ref_text or "").strip())
        with self._cache_lock:
            cached = self._prompts.get(key)
        if cached is not None:
            return cached
        text = (ref_text or "").strip()
        if text:
            prompt = model.create_voice_clone_prompt(
                ref_audio=str(ref_path), ref_text=text, x_vector_only_mode=False
            )
        else:
            # 没有参考文本（音色库里的既有音频）只能走说话人向量模式，音色像、细节少
            prompt = model.create_voice_clone_prompt(
                ref_audio=str(ref_path), ref_text=None, x_vector_only_mode=True
            )
        with self._cache_lock:
            self._prompts = {k: v for k, v in self._prompts.items() if k[0] == id(model)}
            self._prompts[key] = prompt
        return prompt

    def _drop_prompts(self) -> None:
        with self._cache_lock:
            self._prompts.clear()

    # ---------------------------------------------------------------- 按描述生成（主路径）

    def design(self, *, text: str, instruct: str, lang: str = "ZH") -> tuple[bytes, int, str]:
        """按描述生成一段音频，返回 (wav 字节, 采样率, 实际说的文本)。

        同一段逻辑既服务于"角色试听"，也服务于逐句合成。
        """
        model = self._model("design")
        wavs, sample_rate = model.generate_voice_design(
            text=text,
            language=language_name(lang),
            instruct=(instruct or "").strip() or FALLBACK_INSTRUCT,
        )
        return _wav_bytes(wavs[0], sample_rate), int(sample_rate), text

    def _design_batch(self, requests: list[SynthesisRequest], out_paths: list[Path]) -> list[float]:
        model = self._model("design")
        texts = [item.text for item in requests]
        instructs = [(item.voice_prompt or "").strip() or FALLBACK_INSTRUCT for item in requests]
        languages = [language_name(item.lang) for item in requests]
        wavs, sample_rate = model.generate_voice_design(
            text=texts,
            language=languages if len(set(languages)) > 1 else languages[0],
            instruct=instructs,
        )
        durations: list[float] = []
        for waveform, path in zip(wavs, out_paths):
            audio = _wav_bytes(waveform, sample_rate)
            Path(path).write_bytes(audio)
            durations.append(_wav_seconds(audio))
        logger.info("批量按描述生成 %d 条完成", len(requests))
        return durations

    # ---------------------------------------------------------------- 合成

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        started = time.monotonic()
        if request.ref_path:
            model = self._model("base")
            prompt = self._prompt(model, Path(request.ref_path), request.ref_text)
            wavs, sample_rate = model.generate_voice_clone(
                text=request.text,
                language=language_name(request.lang),
                voice_clone_prompt=prompt,
            )
            audio = _wav_bytes(wavs[0], sample_rate)
        else:
            audio, sample_rate, _ = self.design(
                text=request.text, instruct=request.voice_prompt, lang=request.lang
            )
        return SynthesisResult(
            audio=audio,
            duration_sec=_wav_seconds(audio),
            sample_rate=int(sample_rate),
            engine=self.name,
            engine_version=self.version,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def synthesize_batch(self, requests: list[SynthesisRequest], out_paths: list[Path]) -> list[float]:
        """一个包一次解码：描述模式下每条的 instruct 各自不同（模型原生支持列表入参）。"""
        if not requests:
            return []
        if len(out_paths) != len(requests):
            raise ValueError("requests 与 out_paths 数量不一致")
        first = requests[0]
        if any(bool(item.ref_path) != bool(first.ref_path) for item in requests):
            raise ValueError("一个批量包里不能混用参考音频和纯描述")
        if not first.ref_path:
            return self._design_batch(requests, out_paths)
        if any(item.ref_path != first.ref_path for item in requests):
            raise ValueError("批量克隆要求同一个 refId（同一音色）")
        model = self._model("base")
        prompt = self._prompt(model, Path(first.ref_path), first.ref_text)
        languages = [language_name(item.lang) for item in requests]
        wavs, sample_rate = model.generate_voice_clone(
            text=[item.text for item in requests],
            language=languages if len(set(languages)) > 1 else languages[0],
            voice_clone_prompt=prompt,
        )
        durations: list[float] = []
        for waveform, path in zip(wavs, out_paths):
            audio = _wav_bytes(waveform, sample_rate)
            Path(path).write_bytes(audio)
            durations.append(_wav_seconds(audio))
        logger.info("批量克隆 %d 条完成（ref=%s）", len(requests), first.ref_path)
        return durations

    # ---------------------------------------------------------------- 自检

    def recommended_concurrency(self) -> int:
        return max(1, int(self.settings.max_concurrency or self.DEFAULT_CONCURRENCY))

    def capabilities(self) -> dict:
        return {
            "engine": self.name,
            "engineVersion": self.version,
            # 情绪不再走向量/文本通道：语气由"音色描述"（基础描述 + 本句描述）决定
            "emotions": False,
            "emotionDims": [],
            "emotionText": False,
            "rate": False,
            "pronunciation": False,
            "languages": ["ZH", "EN", "JP", "KR", "DE", "FR", "RU", "PT", "ES", "IT"],
            "sampleRate": DEFAULT_SAMPLE_RATE,
            "maxTextChars": int(self.settings.max_text_chars),
            "batch": True,
            "maxBatchItems": int(self.settings.max_batch_items),
            "supportsSeed": False,
            "supportsWarmup": True,
            # 按描述生成（逐句 instruct）+ 试听设计接口
            "voiceDesign": True,
            "voicePrompt": True,
            # 有参考音频时也能克隆（库存音色/手选音色的备用通道）
            "reference": True,
            "backends": {
                "design": self._design_id,
                "clone": self._base_id,
            },
        }

    def memory_report(self) -> dict:
        from ..indextts_compat import host_memory_report

        return {
            "backend": self.name,
            "loaded": self.is_loaded(),
            "loadedModels": sorted(self._models),
            "cachedPrompts": len(self._prompts),
            "host": host_memory_report(),
        }
