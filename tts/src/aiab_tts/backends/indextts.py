import io
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

    def __init__(self, settings):
        self.settings = settings
        self._tts = None

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
                "（uv pip install --python tts/.venv -e third_party/index-tts）。"
            ) from exc

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

        self._tts = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"),
            model_dir=str(model_dir),
            use_bf16=self.settings.use_bf16,
        )
        logger.info("IndexTTS-2.5 已加载：%s", model_dir)

    def unload(self) -> None:
        self._tts = None
        from ..state import _release_gpu_memory

        _release_gpu_memory()

    def recommended_concurrency(self) -> int:
        if self.settings.max_concurrency:
            return int(self.settings.max_concurrency)
        total = _vram_total_mb(self.settings)
        if total >= 16000:
            return 3
        if total >= 10000:
            return 2
        return 1

    def capabilities(self) -> dict:
        return {
            "engine": self.name,
            "engineVersion": self.version,
            "emotions": True,
            "emotionDims": list(EMOTION_DIMS),
            "rate": True,
            "rateRange": [0.5, 2.0],
            "pronunciation": True,
            "pronunciationStyles": ["pinyin", "cmu", "kana"],
            "languages": ["ZH", "EN", "JP", "ES", "AR"],
            "sampleRate": 22050,
            "maxTextChars": self.settings.max_text_chars,
            "supportsSeed": False,
            "supportsWarmup": True,
        }

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if self._tts is None:
            self.load()
        started = time.monotonic()
        text = apply_pronunciation(request.text, request.pronunciation)
        duration_factor = 1.0 / (request.rate or 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.wav"
            self._tts.infer(
                spk_audio_prompt=str(request.ref_path),
                text=text,
                lang=request.lang,
                output_path=str(out_path),
                emo_vector=list(request.emo_vector) if request.emo_vector else None,
                duration_factor=duration_factor,
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
