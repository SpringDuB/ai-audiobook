import io
import json
import logging
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

from .base import SynthesisRequest, SynthesisResult

logger = logging.getLogger(__name__)

EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")


def apply_pronunciation(text: str, mapping: dict[str, str]) -> str:
    """把注音词表注入成 IndexTTS 的 <词|读音> 内联写法；长词优先避免子串抢占。"""
    words = [word for word in mapping if word]
    if not words:
        return text
    pattern = re.compile("|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)))

    # 先屏蔽已有的 <词|读音> 标记，避免把标记里的字再包一层
    shielded: list[str] = []

    def _shield(match: re.Match) -> str:
        shielded.append(match.group(0))
        return f"\x00{len(shielded) - 1}\x00"

    protected = re.sub(r"<[^>]*>", _shield, text)
    # 单遍替换：新插入的标记不会被同一遍扫描再次命中
    result = pattern.sub(lambda match: f"<{match.group(0)}|{mapping[match.group(0)]}>", protected)
    for index, original in enumerate(shielded):
        result = result.replace(f"\x00{index}\x00", original)
    return result


def _vram_total_mb(settings) -> int:
    from ..state import gpu_info

    return int(gpu_info(settings).get("vramTotalMB") or 0)


def _wav_seconds(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload)) as handle:
        return handle.getnframes() / float(handle.getframerate())


class IndexTtsBackend:
    name = "indextts-2.5"
    version = "2.5.0"
    # 并发度：IndexTTS 的 GPT 推理模型有一个实例级条件嵌入槽位，我们打了线程本地补丁
    # （见 indextts_compat.make_gpt_inference_thread_safe）之后可以真并发。
    DEFAULT_CONCURRENCY = 3

    def __init__(self, settings):
        self.settings = settings
        self._tts = None
        # None=还没加载（补丁状态未知）；False=补丁没打上，退回单并发
        self._thread_safe: bool | None = None

    def is_loaded(self) -> bool:
        return self._tts is not None

    def load(self) -> None:
        # 不卡 Python 版本：装得上就能跑，装不上让 import 自己说话
        try:
            from indextts.infer_v2_5 import IndexTTS2  # 懒加载：只有真后端才 import 模型栈
        except ImportError as exc:
            raise RuntimeError(
                f"IndexTTS 不可用：{exc}。"
                "请按 docs/tts-deploy.md 在 tts/.venv 里装好 index-tts"
                "（uv sync --python 3.11 --extra indextts --extra download 后 "
                "uv pip install --python tts/.venv -e index-tts）。"
                "注意 index-tts 自己声明 requires-python >=3.10,<3.12，所以它的 venv 要用 3.11 建。"
            ) from exc

        # 上游别名表缺 BigVGAN：不补就会先 404、再退回很慢的 hf-mirror
        from ..indextts_compat import apply_modelscope_aliases

        apply_modelscope_aliases()

        try:
            import torch  # index-tts 的依赖；这里只用来判断有没有 CUDA

            if not torch.cuda.is_available():
                logger.warning(
                    "torch 看不到 CUDA（torch=%s，可能是 CPU 版）：IndexTTS 会退回 CPU 推理，慢几十倍。"
                    "装 CUDA 版：uv pip install --python tts/.venv "
                    "--index-url https://download.pytorch.org/whl/cu128 torch==2.8.* torchaudio==2.8.*",
                    torch.__version__,
                )
        except ImportError:  # pragma: no cover - index-tts 自身依赖 torch
            pass

        from ..download import ModelIntegrityError, ensure_model

        model_dir = Path(self.settings.model_dir)
        logger.info(
            "准备模型：source=%s dir=%s（缺失时按来源自动下载）", self.settings.model_source, model_dir
        )
        try:
            report = ensure_model(self.settings)
        except ModelIntegrityError as exc:
            raise RuntimeError(f"模型不可用：{exc}") from exc
        for warning in report.get("warnings") or []:
            logger.warning("%s", warning)
        logger.info(
            "模型就绪：%s（source=%s verified=%s）", report["path"], report["source"], report["verified"]
        )

        from ..indextts_compat import (
            apply_bf16_modules,
            apply_cfm_speed,
            effective_tuning,
            fast_model_loading,
            make_conditioning_cache_per_voice,
            make_gpt_inference_thread_safe,
            upstream_supports_speed_patch,
        )

        # 加载期优化：权重 mmap 直读 + 大模型直接在显存上构造（不再"CPU 一份 + 显存一份"）
        with fast_model_loading(self.settings.device):
            self._tts = IndexTTS2(
                cfg_path=str(model_dir / "config.yaml"),
                model_dir=str(model_dir),
                use_bf16=self.settings.use_bf16,
                use_qwen_emo=self.settings.use_qwen_emo,
            )

        self._thread_safe = make_gpt_inference_thread_safe()
        if not self._thread_safe:
            logger.warning("并发补丁没打上：IndexTTS 的条件嵌入槽位仍是实例级，服务退回单并发")
        # 速度旋钮：CFM 迭代步数 / CFG 强度（GPT 束宽在请求里传）
        native_speed = upstream_supports_speed_patch()
        if native_speed:
            # 上游副本已内置：先打标记，别让运行时补丁再套一层
            self._tts._aiab_voice_cache_native = True
            self._tts._aiab_cfm_speed_native = True
        # 参考音频条件按音色缓存：多角色书不必每句重算参考，也不会反复清显存池
        make_conditioning_cache_per_voice(self._tts)
        # 大模块降 bf16（默认只降 w2v-bert）：显存省一半，并发更稳
        apply_bf16_modules(self._tts, self.settings.use_bf16)
        if native_speed:
            self._sync_native_tuning()
        apply_cfm_speed(self._tts, self.tuning)
        # 加载完自检一次：权重该在显存，主机内存只留运行时（见 /debug/memory）
        logger.info("内存自检：%s", json.dumps(self.memory_report(), ensure_ascii=False))
        logger.info(
            "IndexTTS-2.5 已加载：%s（情绪通道：%s）",
            model_dir,
            "文本描述 emoText + 8 维向量 emoVector" if self.settings.use_qwen_emo else "8 维向量 emoVector",
        )

    def unload(self) -> None:
        self._tts = None
        from ..state import _release_gpu_memory

        _release_gpu_memory()

    def memory_report(self) -> dict:
        """权重分布 + 进程内存快照：cpuResidentMB 应当是 0。"""
        from ..indextts_compat import host_memory_report, model_memory_report

        report: dict = {"backend": self.name, "loaded": self.is_loaded(), "host": host_memory_report()}
        if self._tts is not None:
            report["model"] = model_memory_report(self._tts)
        return report

    def tuning(self) -> dict:
        from ..indextts_compat import effective_tuning

        return effective_tuning(self.settings)

    def _sync_native_tuning(self) -> None:
        """上游副本已内置可调步数：把当前旋钮写进实例属性。"""
        tuning = self.tuning()
        self._tts.diffusion_steps = int(tuning["diffusionSteps"])
        self._tts.inference_cfg_rate = float(tuning["cfgRate"])

    def recommended_concurrency(self) -> int:
        """并发上限：默认 3，可用 AIAB_TTS_MAX_CONCURRENCY 调。

        以前并发会 500，是因为 GPT 推理模型用实例属性存"当前请求的条件嵌入"；打上线程本地
        补丁后可以真并发。补丁失败（上游结构变了）时退回 1，宁可慢也不能算错。
        """
        if self._thread_safe is False:
            return 1
        return max(1, int(self.settings.max_concurrency or self.DEFAULT_CONCURRENCY))

    def capabilities(self) -> dict:
        from ..indextts_compat import upstream_supports_batch

        batch_ok = upstream_supports_batch()
        return {
            "engine": self.name,
            "engineVersion": self.version,
            "emotions": True,
            "emotionDims": list(EMOTION_DIMS),
            "emotionText": bool(self.settings.use_qwen_emo),
            "rate": True,
            "rateRange": [0.5, 2.0],
            "pronunciation": True,
            "pronunciationStyles": ["pinyin", "cmu", "kana"],
            "languages": ["ZH", "EN", "JP", "ES", "AR"],
            "sampleRate": 22050,
            "maxTextChars": self.settings.max_text_chars,
            # 批量合成：客户端可把同音色的连续几句打包成一次请求（GPT 只解码一遍）。
            # 只有装着的上游真带 infer_batch 才声明支持，否则客户端会白撞一次批量接口。
            "batch": batch_ok,
            "maxBatchItems": int(self.settings.max_batch_items) if batch_ok else 1,
            # 传 seed 时后端会先给 torch 播种再推理：同一 seed + 同一输入可复现（A/B 对比用）
            "supportsSeed": True,
            "supportsWarmup": True,
        }

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if self._tts is None:
            self.load()
        started = time.monotonic()
        text = apply_pronunciation(request.text, request.pronunciation)
        duration_factor = 1.0 / (request.rate or 1.0)
        # 文本描述优先（要服务端加载了 QwenEmotion），否则退回 8 维向量
        use_text = bool(self.settings.use_qwen_emo and request.emotion_text)
        if request.seed is not None:
            import torch

            torch.manual_seed(int(request.seed))
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(int(request.seed))
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.wav"
            tuning = self.tuning()
            if getattr(self._tts, "_aiab_cfm_speed_native", False):
                self._sync_native_tuning()
            self._tts.infer(
                spk_audio_prompt=str(request.ref_path),
                text=text,
                lang=request.lang,
                output_path=str(out_path),
                emo_vector=list(request.emo_vector) if (request.emo_vector and not use_text) else None,
                use_emo_text=use_text,
                emo_text=request.emotion_text if use_text else None,
                duration_factor=duration_factor,
                # 束宽 1 = 纯采样：GPT 解码开销直接降到 1/num_beams
                num_beams=int(tuning["numBeams"]),
            )
            audio = out_path.read_bytes()
        return SynthesisResult(
            audio=audio,
            duration_sec=_wav_seconds(audio),
            sample_rate=22050,
            engine=self.name,
            engine_version=self.version,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def synthesize_batch(self, requests: list[SynthesisRequest], out_paths: list[Path]) -> list[float]:
        """同一个音色的多条文本一次解码（批量）。返回每条音频的时长。

        GPT 自回归解码是显存带宽瓶颈（每步读一遍权重），一批 N 条只读一次 → GPT 段耗时
        几乎不随 N 增长。s2mel / 声码器仍逐条跑，所以整体大约快 1.5~2x。
        """
        if not requests:
            return []
        if len(out_paths) != len(requests):
            raise ValueError("requests 与 out_paths 数量不一致")
        if self._tts is None:
            self.load()
        if not hasattr(self._tts, "infer_batch"):
            raise RuntimeError("上游缺少 infer_batch（tts/index-tts 未打 AIAB 补丁）")
        first = requests[0]
        if any(item.ref_path != first.ref_path for item in requests):
            raise ValueError("批量合成要求同一个 refId（同一音色）")
        tuning = self.tuning()
        # 采样种子是进程级状态，批量里只认第一条的种子（批量解码本身也会改变采样结果）
        first_seed = next((item.seed for item in requests if item.seed is not None), None)
        if first_seed is not None:
            import torch

            torch.manual_seed(int(first_seed))
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(int(first_seed))
        durations = self._tts.infer_batch(
            spk_audio_prompt=str(first.ref_path),
            texts=[apply_pronunciation(item.text, item.pronunciation) for item in requests],
            output_paths=[str(path) for path in out_paths],
            lang=first.lang,
            emo_vectors=[list(item.emo_vector) if item.emo_vector else None for item in requests],
            duration_factors=[1.0 / (item.rate or 1.0) for item in requests],
            num_beams=int(tuning["numBeams"]),
        )
        return [float(value) for value in durations]
