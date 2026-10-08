import logging
import os
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

from .. import audio, store
from ..text.chunking import chunk_text
from .base import AudioResult, EngineCapabilities, SynthParams
from .errors import TtsBadRef, TtsError, TtsVoiceMissing, classify_status

logger = logging.getLogger(__name__)


class RefCache:
    """进程内 refId 缓存：同一个 (端点, 音色) 只上传一次参考音频。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def key(self, base_url: str, voice_id: str, path: Path, ref_text: str = "") -> tuple[str, str]:
        stat = path.stat()
        # 参考文本也进 key：同一段音频配不同文字，克隆出来的语气不一样
        return (base_url, f"{voice_id}:{stat.st_mtime_ns}:{stat.st_size}:{hash(ref_text)}")

    def get(self, key) -> str | None:
        return self._data.get(key)

    def set(self, key, ref_id: str) -> None:
        self._data[key] = ref_id

    def drop(self, key) -> None:
        self._data.pop(key, None)


class HttpTtsEngine:
    """单个 TTS 服务端点的客户端（多端点分发由 TtsPool 负责）。"""

    CHUNK_PAUSE_MS = 120

    def __init__(self, base_url, settings, transport=None, ref_cache=None, timeout=None):
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        self._caps: EngineCapabilities | None = None
        self._refs = ref_cache or RefCache()
        self._client = httpx.Client(
            base_url=self.base_url + "/",
            timeout=timeout
            or httpx.Timeout(settings.tts_timeout_seconds, connect=settings.tts_connect_timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict:
        return self._get_json("/health")

    def recommended_concurrency(self) -> int:
        try:
            return int(self.health().get("recommendedConcurrency") or 0)
        except (TtsError, ValueError, TypeError):
            return 0

    def capabilities(self) -> EngineCapabilities:
        if self._caps is None:
            data = self._get_json("/capabilities")
            self._caps = EngineCapabilities(
                name=str(data.get("engine") or "tts"),
                version=str(data.get("engineVersion") or "unknown"),
                emotions=bool(data.get("emotions")),
                emotion_dims=tuple(data.get("emotionDims") or ()),
                emotion_text=bool(data.get("emotionText")),
                rate=bool(data.get("rate")),
                pronunciation=bool(data.get("pronunciation")),
                sample_rate=int(data.get("sampleRate") or 22050),
                max_text_chars=int(data.get("maxTextChars") or 300),
                batch=bool(data.get("batch")),
                max_batch_items=max(1, int(data.get("maxBatchItems") or 1)),
                voice_design=bool(data.get("voiceDesign")),
                voice_prompt=bool(data.get("voicePrompt")),
            )
        return self._caps

    def design_voice(
        self, *, instruct: str, text: str, lang: str, out_path: Path, timeout: float | None = None
    ) -> AudioResult:
        """按描述设计一个音色，把生成的参考音频落到 out_path（Qwen3-TTS VoiceDesign）。"""
        if not self.capabilities().voice_design:
            raise TtsError("服务端不支持音色设计（需要 TTS backend=qwen3）")
        payload = {"instruct": instruct, "text": text, "lang": lang or "ZH"}
        try:
            response = self._client.post(
                "v1/design",
                json=payload,
                timeout=timeout or httpx.Timeout(self.settings.tts_design_timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise TtsError(f"音色设计超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TtsError(f"音色设计请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        return self._store_wav(response, out_path)

    def synthesize_batch(
        self, items: list[tuple[str, SynthParams]], voice_id: str, out_paths: list[Path]
    ) -> list[AudioResult]:
        """同一个音色的多条文本一次解码（服务端批量接口），返回每条的结果。

        调用方要保证：items 与 out_paths 一一对应、同一个音色、每条都不需要客户端分块
        （太长的文本请走 ``synthesize``，分块的时序逻辑在那边）。
        """
        caps = self.capabilities()
        if not caps.batch:
            raise TtsError("服务端不支持批量合成")
        if not items or len(items) != len(out_paths):
            raise TtsError("批量合成的 items 与 out_paths 数量不一致")
        payload_items = []
        for text, params in items:
            params = params or SynthParams()
            entry: dict = {"text": text, "rate": params.rate}
            if params.voice_prompt and caps.voice_prompt:
                entry["voicePrompt"] = params.voice_prompt
            elif params.emotion_text and caps.emotion_text:
                entry["emoText"] = params.emotion_text
            elif params.emo_vector:
                entry["emoVector"] = list(params.emo_vector)
            if params.pronunciation:
                entry["pronunciation"] = params.pronunciation
            payload_items.append(entry)
        first_params = items[0][1] or SynthParams()
        payload: dict = {"lang": first_params.lang or "ZH", "items": payload_items}
        ref_id = self._optional_ref_id(voice_id, first_params)
        if ref_id:
            payload["refId"] = ref_id
        try:
            response = self._post_batch(payload)
        except TtsBadRef:
            if not ref_id:
                raise
            self._refs.drop(self._ref_key(voice_id, params=first_params))
            payload["refId"] = self._ref_id(voice_id, params=first_params)
            response = self._post_batch(payload)
        durations = [
            float(value)
            for value in (response.headers.get("X-Item-Durations") or "").split(",")
            if value.strip()
        ]
        results: list[AudioResult] = []
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            for index, out_path in enumerate(out_paths):
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = out_path.with_name(out_path.name + ".tmp")
                with archive.open(f"{index:03d}.wav") as source, open(tmp, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, out_path)
                duration = durations[index] if index < len(durations) else audio.wav_duration(out_path)
                results.append(
                    AudioResult(path=out_path, duration=duration, sample_rate=caps.sample_rate)
                )
        return results

    def _post_batch(self, payload: dict) -> httpx.Response:
        try:
            response = self._client.post("v1/synthesize_batch", json=payload)
        except httpx.TimeoutException as exc:
            raise TtsError(f"批量合成超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TtsError(f"批量合成请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        return response

    def synthesize(self, text: str, voice_id: str, params: SynthParams | None, out_path: Path) -> AudioResult:
        out_path = Path(out_path)
        params = params or SynthParams()
        limit = self.settings.tts_max_line_chunk_chars or self.capabilities().max_text_chars
        chunks = chunk_text(text, limit) if limit and len(text) > limit else [text]
        if len(chunks) <= 1:
            return self._synthesize_once(text, voice_id, params, out_path)

        parts: list[tuple[Path, int]] = []
        for position, chunk in enumerate(chunks, start=1):
            part_path = out_path.with_name(f"{out_path.stem}.part{position:02d}.wav")
            self._synthesize_once(chunk, voice_id, params, part_path)
            # 最后一块之后不留静音，避免给下游字幕时间轴多加一段尾巴
            pause = self.CHUNK_PAUSE_MS if position < len(chunks) else 0
            parts.append((part_path, pause))
        try:
            duration = audio.concat_with_pauses(parts, out_path)
        finally:
            for part_path, _ in parts:
                part_path.unlink(missing_ok=True)
        return AudioResult(path=out_path, duration=duration, sample_rate=self.capabilities().sample_rate)

    # --- 内部 ---

    def _synthesize_once(self, text: str, voice_id: str, params: SynthParams, out_path: Path) -> AudioResult:
        caps = self.capabilities()
        payload = {
            "text": text,
            "lang": params.lang or "ZH",
            "rate": params.rate,
            "format": "wav",
        }
        ref_id = self._optional_ref_id(voice_id, params)
        if ref_id:
            payload["refId"] = ref_id
        if params.voice_prompt and caps.voice_prompt:
            # 逐句描述（角色基础描述 + 本句语气）：Qwen3-TTS 按它演这一句
            payload["voicePrompt"] = params.voice_prompt
        elif params.emotion_text and caps.emotion_text:
            payload["emoText"] = params.emotion_text
        elif params.emo_vector:
            payload["emoVector"] = list(params.emo_vector)
        if params.pronunciation:
            payload["pronunciation"] = params.pronunciation
        try:
            response = self._post_synthesize(payload)
        except TtsBadRef:
            if not ref_id:
                raise
            self._refs.drop(self._ref_key(voice_id, params=params))
            payload["refId"] = self._ref_id(voice_id, params=params)
            response = self._post_synthesize(payload)
        return self._store_wav(response, out_path)

    def _post_synthesize(self, payload: dict) -> httpx.Response:
        try:
            response = self._client.post("v1/synthesize", json=payload)
        except httpx.TimeoutException as exc:
            raise TtsError(f"合成超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TtsError(f"合成请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        return response

    def _store_wav(self, response: httpx.Response, out_path: Path) -> AudioResult:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(response.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, out_path)
        header = response.headers.get("X-Duration-Sec")
        duration = float(header) if header else audio.wav_duration(out_path)
        sample_rate = int(response.headers.get("X-Sample-Rate") or self.capabilities().sample_rate)
        return AudioResult(path=out_path, duration=duration, sample_rate=sample_rate)

    def _ref_path(self, voice_id: str, params: SynthParams | None) -> Path:
        """这一行用哪个参考音频：行参数里带了就用它，否则到音色库里按 voice_id 找。"""
        override = getattr(params, "ref_path", None) if params is not None else None
        return Path(override) if override else store.voice_ref_path(self.settings, voice_id)

    def _optional_ref_id(self, voice_id: str, params: SynthParams | None) -> str | None:
        """要参考音频就上传并返回 refId；纯描述通道返回 None（不上传、也不需要音色库）。

        三种情况：
          - 行参数里带了参考音频（手工选的库存音色）→ 上传它；
          - 只有音色描述、服务端支持按描述生成 → 不需要参考音频；
          - 其余（旁白库存音色等）→ 按 voice_id 到音色库里找，缺文件就报 TtsVoiceMissing。
        """
        if params is not None and params.ref_path is not None:
            return self._ref_id(voice_id, params)
        if params is not None and params.voice_prompt and self.capabilities().voice_prompt:
            return None
        return self._ref_id(voice_id, params)

    def _ref_key(self, voice_id: str, params: SynthParams | None = None):
        path = self._ref_path(voice_id, params)
        if not path.exists():
            raise TtsVoiceMissing(f"缺少参考音频: {path}")
        ref_text = (getattr(params, "ref_text", "") or "") if params is not None else ""
        return self._refs.key(self.base_url, voice_id, path, ref_text)

    def _ref_id(self, voice_id: str, params: SynthParams | None = None) -> str:
        key = self._ref_key(voice_id, params)
        cached = self._refs.get(key)
        if cached:
            return cached
        path = self._ref_path(voice_id, params)
        ref_text = (getattr(params, "ref_text", "") or "") if params is not None else ""
        try:
            response = self._client.post(
                "v1/refs",
                files={"file": (path.name, path.read_bytes(), "audio/wav")},
                # 参考文本（克隆模型要它来对齐内容）：设计出来的音色知道说的是哪句，
                # 库存音色没有转写就给空串 —— 服务端会自动退回说话人向量模式
                data={"refText": ref_text},
                timeout=self.settings.tts_ref_upload_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TtsError(f"上传参考音频失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        ref_id = (response.json() or {}).get("refId")
        if not ref_id:
            raise TtsError("参考音频上传响应缺少 refId")
        self._refs.set(key, ref_id)
        return ref_id

    def _get_json(self, path: str) -> dict:
        try:
            response = self._client.get(path.lstrip("/"))
        except httpx.HTTPError as exc:
            raise TtsError(f"{path} 请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        return response.json()

    @staticmethod
    def _error(response: httpx.Response) -> TtsError:
        code = None
        message = response.text[:200]
        try:
            detail = response.json().get("detail")
            if isinstance(detail, dict):
                code = detail.get("code")
                message = detail.get("message") or message
        except Exception:  # noqa: BLE001 - 非 JSON 错误体也要能报错
            pass
        error_cls = classify_status(response.status_code, code)
        return error_cls(f"{code or response.status_code}: {message}")
