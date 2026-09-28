import logging
import os
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

    def key(self, base_url: str, voice_id: str, path: Path) -> tuple[str, str]:
        stat = path.stat()
        return (base_url, f"{voice_id}:{stat.st_mtime_ns}:{stat.st_size}")

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
            )
        return self._caps

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
        payload = {
            "text": text,
            "refId": self._ref_id(voice_id),
            "lang": params.lang or "ZH",
            "rate": params.rate,
            "format": "wav",
        }
        if params.emotion_text and self.capabilities().emotion_text:
            payload["emoText"] = params.emotion_text
        elif params.emo_vector:
            payload["emoVector"] = list(params.emo_vector)
        if params.pronunciation:
            payload["pronunciation"] = params.pronunciation
        try:
            response = self._post_synthesize(payload)
        except TtsBadRef:
            self._refs.drop(self._ref_key(voice_id))
            payload["refId"] = self._ref_id(voice_id)
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

    def _ref_key(self, voice_id: str):
        path = store.voice_ref_path(self.settings, voice_id)
        if not path.exists():
            raise TtsVoiceMissing(f"缺少参考音频: {path}")
        return self._refs.key(self.base_url, voice_id, path)

    def _ref_id(self, voice_id: str) -> str:
        key = self._ref_key(voice_id)
        cached = self._refs.get(key)
        if cached:
            return cached
        path = store.voice_ref_path(self.settings, voice_id)
        try:
            response = self._client.post(
                "v1/refs",
                files={"file": (path.name, path.read_bytes(), "audio/wav")},
                data={"refText": ""},
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
