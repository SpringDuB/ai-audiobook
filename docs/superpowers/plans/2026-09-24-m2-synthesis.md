# AI 有声书 M2 合成链 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M1 产出的逐句标注真正读成音频：起一个**独立部署的 TTS 服务**（IndexTTS-2.5，支持 modelscope/huggingface/local 三种模型来源），后端按服务端自报的并发上限投递、按行缓存、断点续跑，并能给出"改一句只重算一句"的证据。

**Architecture:** 仓库内两个独立项目：后端（根 `pyproject.toml`，Python 3.13）与 TTS 服务（`tts/`，独立 `pyproject.toml` + 独立 venv + 独立进程）。两者只通过冻结的 HTTP 契约通信（`/health`、`/capabilities`、`/v1/refs`、`/v1/synthesize`、`/warmup`、`/unload`）。后端侧是"端点池 + 适配器"：池负责多实例分发、并发自报聚合、5xx/OOM 熔断降档；适配器负责"文本分块 → 上传参考音频拿 refId → 合成 → 原子落盘"，对上层暴露与 `FakeEngine` 完全相同的接口。

**Tech Stack:** 后端 Python 3.13 + httpx（已有）；TTS 服务 Python ≥3.10（真实 IndexTTS-2.5 部署用 3.10–3.11）+ FastAPI + uvicorn + uv；测试用 pytest + httpx.MockTransport + 服务端 fake 后端（不依赖 GPU、不下载模型）。

**Spec:** `docs/superpowers/specs/2026-09-24-ai-audiobook-design.md`（§6 合成/缓存/断点、§7 引擎适配层与 TTS 服务、§3.3 并发与显存保护、§7.4 多实例与生命周期）

## Global Constraints

- 后端 Python 3.13；TTS 服务独立项目、独立 venv、独立进程，后端**不得** import 任何 `aiab_tts` 代码，也不得在 `serve`/`worker` 进程内加载模型。
- TTS 并发上限**由服务端 `/health` 自报**（`recommendedConcurrency`），客户端不得写死；客户端只保留安全上限 `AB_SYNTH_CONCURRENCY_MAX`（默认 16）。
- 缓存键必须是 `sha256(文本 + 音色ID + 引擎名 + 引擎版本 + 引擎实际支持的参数)`；引擎不支持的参数不得计入（M0 已实现，本计划不改语义）。
- 参考音频**不逐句上传**：每个 (端点, 音色) 首次上传得到 `refId`，之后只传 `refId`。
- 所有落盘原子：音频写 `<lineId>.wav.tmp` → `os.replace`；`.meta.json` 后写。
- 失败必须可见：每行失败写 `issues.jsonl`，降级/熔断写结构化日志，绝不静默跳过。
- 队列、租约、取消语义沿用 M0/M1，不引入 Redis/Celery。
- 每个 commit 的消息末尾追加一行：`Co-authored-by: Codex <codex@openai.com>`。
- 运行环境以 Windows + PowerShell 为准；命令写成 PowerShell 可直接执行的形式。

## 本计划的范围

| 做 | 不做（归属） |
|---|---|
| TTS 服务 HTTP 契约与两种后端（fake / indextts-2.5） | 停顿响度归一、mkv 封装、整本合本（P4/M3） |
| 模型来源三选一 + 权重校验 + 断点续传 | 96 音色迁移（P7/M6）——本计划只定义 `data/voices/<id>/ref.wav` 读取约定 |
| 后端端点池、并发自报聚合、熔断降档、行级缓存与续跑 | 浏览器 UI（P5/M4） |
| 本地无 GPU 端到端验收（fake 后端）+ GPU 机器部署清单 | 配图视频（P6/M5） |

M2 结束时：`uv run aiab worker` 能把一本书的每一行投给 TTS 服务、落成 `audio/chapter_XXXX/<lineId>.wav` + `.meta.json`，章节 `wav/srt` 时间轴与真实音频时长一致；改一句只重算那一行；TTS 服务可单独启停、可放另一台机器。

---

## 冻结契约（前后端唯一耦合面，实现时不得改字段名）

### `GET /health`

```json
{
  "status": "ok",
  "modelLoaded": true,
  "device": "cuda:0",
  "vramTotalMB": 24576,
  "vramUsedMB": 8123,
  "recommendedConcurrency": 3,
  "inflight": 0,
  "avgInferenceSecPerAudioSec": 0.42,
  "engine": "indextts-2.5",
  "engineVersion": "2.5.0",
  "modelSource": "local",
  "uptimeSec": 128
}
```

`status ∈ ok | loading | unloaded | error`。`recommendedConcurrency` 是"这台机器能跑多少"的容量，
**冷启动（unloaded）时也必须如实上报**；报 0 会让后端池把它当成故障端点，从而永远触发不了首次加载
（这是实施时发现并修正的契约细节）。后端池把 `ok | loading | unloaded` 都视为可用，只有 `error` 视为不可用。

### `GET /capabilities`

```json
{
  "engine": "indextts-2.5",
  "engineVersion": "2.5.0",
  "emotions": true,
  "emotionDims": ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"],
  "rate": true,
  "rateRange": [0.5, 2.0],
  "pronunciation": true,
  "pronunciationStyles": ["pinyin", "cmu", "kana"],
  "languages": ["ZH", "EN", "JP", "ES", "AR"],
  "sampleRate": 22050,
  "maxTextChars": 300,
  "supportsSeed": true,
  "supportsWarmup": true
}
```

### `POST /v1/refs`（multipart：`file`、可选 `refText`）

```json
{"refId": "ref_3f2a", "durationSec": 6.4, "sampleRate": 22050}
```

### `POST /v1/synthesize`（JSON）

请求：

```json
{"text": "你好。", "refId": "ref_3f2a", "lang": "ZH",
 "emoVector": [0, 0, 0.8, 0, 0, 0, 0, 0], "rate": 1.0,
 "pronunciation": {"行": "XING2"}, "seed": 7, "format": "wav"}
```

成功：`200` + `audio/wav` 字节流，响应头 `X-Engine`、`X-Engine-Version`、`X-Duration-Sec`、`X-Sample-Rate`、`X-Elapsed-Ms`。

失败：HTTP 错误码 + 统一体 `{"detail": {"code": "...", "message": "..."}}`，`code ∈ bad_request | bad_ref | busy | oom | engine_error | not_loaded`。

**`rate` 的线上语义固定为"语速倍率"（>1 更快）**；引擎侧 `duration_factor` 是它的倒数（`duration_factor = 1 / rate`），换算只属于服务端，后端永远只发 `rate`。

### `POST /warmup` / `POST /unload`

```json
{"ok": true, "modelLoaded": true, "elapsedMs": 18400}
```

### 后端约定的音色目录

```
data/voices/<voiceId>/ref.wav        参考音频（M6 迁移自旧系统的 参考音频.wav）
data/voices/<voiceId>/voice.json     标签（M1 已能读）
```

---

## File Structure

```
ai-audiobook/
  src/audiobook/
    config.py                           (+ engine / tts_* 配置)
    store.py                            (+ voice_ref_path)
    analysis/issues.py                  (+ tts_* 异常类型)
    engines/
      base.py                           (不变)
      fake.py                           (不变)
      errors.py                         TtsError/TtsBusy/TtsOom/TtsUnavailable/TtsBadRef/TtsBadRequest
      http_tts.py                       HttpTtsEngine：单端点客户端 + 参数映射 + 长句分块
      pool.py                           TtsPool：多端点、健康聚合、熔断降档、并发自报
      factory.py                        build_engine(settings)：fake | http
    handlers/synthesize.py              (改) 用引擎自报并发 + 失败分类
    api/app.py                          (+ GET /api/tts/status)
    cli.py                              (+ serve 启动探测；worker 用 build_engine)
  tts/                                  独立项目：独立 pyproject/venv/进程
    pyproject.toml
    src/aiab_tts/
      __init__.py
      config.py                         TtsSettings（backend / model source / 并发 / 端口）
      schemas.py                        请求响应模型（与冻结契约一致）
      app.py                            FastAPI 路由
      state.py                          ServiceState：加载状态、inflight、并发自报
      download.py                       模型来源三选一 + manifest 校验 + 断点续传
      backends/
        __init__.py
        base.py                         TtsBackend 协议 + SynthesisRequest/Result
        fake.py                         无 GPU 后端（生成可播放 WAV，可控延迟/失败）
        indextts.py                     IndexTTS-2.5 封装（懒加载 + 参数映射 + 版本守卫）
      cli.py                            aiab-tts serve|download|check|unload
    tests/
      test_app_contract.py
      test_state_concurrency.py
      test_backend_fake.py
      test_backend_indextts.py
      test_download.py
  tests/
    test_tts_errors.py
    test_http_tts_engine.py
    test_http_tts_chunking.py
    test_tts_pool.py
    test_engine_factory.py
    test_tts_contract_e2e.py            起真实 fake 后端服务（子进程）跑一章
  docs/tts-deploy.md                    GPU 机器部署与验收清单
```

职责边界：

- `http_tts.py` 只认识"一个端点"；`pool.py` 只认识"多个端点怎么挑、怎么降档"；`factory.py` 只认识"配置到引擎实例"。
- `tts/src/aiab_tts/backends/*` 只认识"一段文本 + 参考音频 → 一段波形"；HTTP、并发、下载由外层负责。
- 后端任何地方都不得 import `aiab_tts`。

---

## 任务一览

| # | 任务 | 产出 |
|---|---|---|
| 1 | TTS 错误类型与配置项 | `engines/errors.py`、`config.py`、`store.voice_ref_path`、issue 类型扩展 |
| 2 | 单端点 HTTP 客户端 | `engines/http_tts.py` |
| 3 | 长句分块与拼接 | `http_tts.py` 分块路径 + 测试 |
| 4 | 端点池：并发自报 + 熔断降档 | `engines/pool.py` |
| 5 | 引擎工厂与接线 | `engines/factory.py`、`cli.py`、`handlers/synthesize.py`、`api/app.py` |
| 6 | TTS 服务骨架与 HTTP 契约 | `tts/`（fake 后端 + 自带测试） |
| 7 | 服务端并发与显存保护 | `tts/src/aiab_tts/state.py`、OOM 映射 |
| 8 | 模型来源三选一 + 校验 + 续传 | `tts/src/aiab_tts/download.py` |
| 9 | IndexTTS-2.5 后端 | `tts/src/aiab_tts/backends/indextts.py` |
| 10 | 契约级端到端 + 部署清单 | `tests/test_tts_contract_e2e.py`、`docs/tts-deploy.md` |

---

### Task 1: TTS 错误类型与配置项

**Files:**
- Create: `src/audiobook/engines/errors.py`
- Modify: `src/audiobook/config.py`、`src/audiobook/store.py`、`src/audiobook/analysis/issues.py`
- Test: `tests/test_tts_errors.py`；改 `tests/test_analysis_issues.py`

**Interfaces:**
- Produces:
  - `TtsError(RuntimeError)` / `TtsBusy` / `TtsOom` / `TtsUnavailable` / `TtsBadRef` / `TtsBadRequest` / `TtsVoiceMissing(TtsBadRef)`
  - `classify_status(status_code: int, code: str | None) -> type[TtsError]`：`busy` → `TtsBusy`；`oom` → `TtsOom`；`not_loaded` → `TtsUnavailable`；`bad_ref` → `TtsBadRef`；`bad_request` → `TtsBadRequest`；429 → `TtsBusy`；502/503/504 → `TtsUnavailable`；404 → `TtsBadRef`；400 → `TtsBadRequest`；其余 → `TtsError`
  - `Settings` 新字段：`engine="fake"`、`tts_timeout_seconds=180.0`、`tts_connect_timeout_seconds=5.0`、`tts_ref_upload_timeout_seconds=120.0`、`synth_concurrency_max=16`、`tts_health_cache_seconds=5.0`、`tts_breaker_seconds=60.0`、`tts_max_line_chunk_chars=0`（0 表示用服务端 `maxTextChars`）
  - `store.voice_ref_path(settings, voice_id) -> Path`（`data/voices/<id>/ref.wav`）
  - `ISSUE_KINDS` 追加：`tts_line_failed`、`tts_ref_missing`、`tts_endpoint_down`、`audio_missing`

- [ ] **Step 1: 写失败测试**

`tests/test_tts_errors.py`：

```python
from audiobook import store
from audiobook.analysis.issues import ISSUE_KINDS
from audiobook.config import get_settings
from audiobook.engines.errors import (
    TtsBadRef,
    TtsBadRequest,
    TtsBusy,
    TtsError,
    TtsOom,
    TtsUnavailable,
    TtsVoiceMissing,
    classify_status,
)


def test_classify_status_maps_service_codes_to_typed_errors():
    assert classify_status(429, "busy") is TtsBusy
    assert classify_status(503, "busy") is TtsBusy
    assert classify_status(503, "oom") is TtsOom
    assert classify_status(503, "not_loaded") is TtsUnavailable
    assert classify_status(404, "bad_ref") is TtsBadRef
    assert classify_status(400, "bad_request") is TtsBadRequest
    assert classify_status(500, "engine_error") is TtsError
    assert classify_status(418, None) is TtsError


def test_typed_errors_are_tts_errors():
    for cls in (TtsBusy, TtsOom, TtsUnavailable, TtsBadRef, TtsBadRequest, TtsVoiceMissing):
        assert issubclass(cls, TtsError)
    assert issubclass(TtsVoiceMissing, TtsBadRef)


def test_tts_settings_defaults():
    settings = get_settings()
    assert settings.engine == "fake"
    assert settings.synth_concurrency_max == 16
    assert settings.tts_breaker_seconds == 60.0
    assert settings.tts_max_line_chunk_chars == 0


def test_voice_ref_path_layout(settings):
    assert store.voice_ref_path(settings, "v_x").as_posix().endswith("voices/v_x/ref.wav")


def test_tts_issue_kinds_are_registered():
    for kind in ("tts_line_failed", "tts_ref_missing", "tts_endpoint_down", "audio_missing"):
        assert kind in ISSUE_KINDS
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_tts_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audiobook.engines.errors'`

- [ ] **Step 3: 写最小实现**

`src/audiobook/engines/errors.py`：

```python
class TtsError(RuntimeError):
    """TTS 服务调用失败（网络、协议、引擎内部错误）。"""


class TtsBusy(TtsError):
    """服务端忙/限流：可退避重试。"""


class TtsOom(TtsError):
    """显存不足：必须降档 + 熔断。"""


class TtsUnavailable(TtsError):
    """模型未加载或服务不可达。"""


class TtsBadRef(TtsError):
    """参考音频失效（refId 未知/过期）：重新上传后可重试。"""


class TtsVoiceMissing(TtsBadRef):
    """本地缺少该音色的参考音频文件。"""


class TtsBadRequest(TtsError):
    """请求本身有问题（参数越界、文本为空）：重试无意义。"""


_CODE_TO_ERROR = {
    "busy": TtsBusy,
    "oom": TtsOom,
    "not_loaded": TtsUnavailable,
    "bad_ref": TtsBadRef,
    "bad_request": TtsBadRequest,
}


def classify_status(status_code: int, code: str | None) -> type[TtsError]:
    if code in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[code]
    if status_code == 429:
        return TtsBusy
    if status_code in (502, 503, 504):
        return TtsUnavailable
    if status_code == 404:
        return TtsBadRef
    if status_code == 400:
        return TtsBadRequest
    return TtsError
```

`src/audiobook/config.py`（放在 `synth_concurrency` 之后）：

```python
    engine: str = "fake"
    tts_timeout_seconds: float = 180.0
    tts_connect_timeout_seconds: float = 5.0
    tts_ref_upload_timeout_seconds: float = 120.0
    synth_concurrency_max: int = 16
    tts_health_cache_seconds: float = 5.0
    tts_breaker_seconds: float = 60.0
    tts_max_line_chunk_chars: int = 0
```

`src/audiobook/store.py`（`voice_path` 之后）：

```python
def voice_ref_path(settings, voice_id: str) -> Path:
    return settings.voices_dir / voice_id / "ref.wav"
```

`src/audiobook/analysis/issues.py`：`ISSUE_KINDS` 追加 `"tts_line_failed"`、`"tts_ref_missing"`、`"tts_endpoint_down"`、`"audio_missing"`；同步更新 `tests/test_analysis_issues.py::test_issue_kinds_are_frozen` 的期望元组。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_tts_errors.py tests/test_analysis_issues.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add src/audiobook tests
git commit -m "feat: TTS 错误类型与引擎配置" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 2: 单端点 HTTP 客户端

**Files:**
- Create: `src/audiobook/engines/http_tts.py`
- Modify: `src/audiobook/analysis/issues.py`（如 Task 1 已加则不动）
- Test: `tests/test_http_tts_engine.py`

**Interfaces:**
- Consumes: `EngineCapabilities` / `SynthParams` / `AudioResult`（`engines/base.py`）、`errors.py`、`store.voice_ref_path`、`audio.wav_duration`
- Produces: `HttpTtsEngine(base_url, settings, transport=None, ref_cache=None)`
  - `.health() -> dict`
  - `.capabilities() -> EngineCapabilities`（首次取 `/capabilities` 并缓存；`name=engine`、`version=engineVersion`、`emotion_dims=emotionDims`、`max_text_chars=maxTextChars`、`sample_rate=sampleRate`）
  - `.synthesize(text, voice_id, params, out_path) -> AudioResult`
  - `.close()`、`.recommended_concurrency() -> int`
  - `RefCache`：`get(key)` / `set(key, value)` / `drop(key)`（key = `(base_url, voice_id)`，带 mtime+size 校验，进程内内存即可）

行为契约：

1. 参考音频路径 = `store.voice_ref_path(settings, voice_id)`；文件不存在 → `TtsVoiceMissing`。
2. 首次合成某音色时 `POST /v1/refs`（multipart）拿 `refId` 并缓存；之后只传 `refId`。
3. 服务端返回 `bad_ref`（refId 过期）时**自动重新上传并重试一次**。
4. 非 200 响应解析 `{"detail": {"code": ..., "message": ...}}`，按 `classify_status` 抛对应异常。
5. 成功响应写 `<out_path>`（先写 `.tmp` 再 `os.replace`），时长取响应头 `X-Duration-Sec`，缺失时用 `audio.wav_duration` 兜底。

- [ ] **Step 1: 写失败测试**

`tests/test_http_tts_engine.py`：

```python
import json
import struct
import wave
from pathlib import Path

import httpx
import pytest

from audiobook.engines.base import SynthParams
from audiobook.engines.errors import TtsBusy, TtsOom, TtsVoiceMissing
from audiobook.engines.http_tts import HttpTtsEngine

CAPS = {
    "engine": "indextts-2.5",
    "engineVersion": "2.5.0",
    "emotions": True,
    "emotionDims": ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"],
    "rate": True,
    "rateRange": [0.5, 2.0],
    "pronunciation": True,
    "pronunciationStyles": ["pinyin"],
    "languages": ["ZH"],
    "sampleRate": 22050,
    "maxTextChars": 300,
}


def wav_bytes(seconds: float = 0.1, rate: int = 22050) -> bytes:
    frames = int(rate * seconds)
    buf = b"".join(struct.pack("<h", int(8000 * ((i % 100) - 50) / 50)) for i in range(frames))
    import io

    stream = io.BytesIO()
    with wave.open(stream, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(buf)
    return stream.getvalue()


def make_voice(settings, voice_id="v_test", content=b"RIFFfake"):
    path = settings.voices_dir / voice_id / "ref.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def engine_with(handler, settings, **kwargs) -> HttpTtsEngine:
    return HttpTtsEngine(
        "http://tts.local",
        settings,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_capabilities_are_mapped_and_cached(settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/capabilities"
        return httpx.Response(200, json=CAPS)

    engine = engine_with(handler, settings)
    caps = engine.capabilities()
    assert caps.name == "indextts-2.5" and caps.version == "2.5.0"
    assert caps.emotion_dims[0] == "happy" and caps.sample_rate == 22050
    assert caps.max_text_chars == 300
    engine.capabilities()
    assert calls["n"] == 1


def test_uploads_reference_once_and_reuses_ref_id(settings):
    make_voice(settings)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1", "durationSec": 6.0, "sampleRate": 22050})
        return httpx.Response(
            200,
            content=wav_bytes(),
            headers={"X-Duration-Sec": "0.1", "X-Sample-Rate": "22050", "X-Engine-Version": "2.5.0"},
        )

    engine = engine_with(handler, settings)
    out = Path(settings.data_dir) / "a.wav"
    engine.synthesize("第一句。", "v_test", SynthParams(), out)
    engine.synthesize("第二句。", "v_test", SynthParams(), out)
    assert seen.count("/v1/refs") == 1
    assert seen.count("/v1/synthesize") == 2
    assert out.exists()


def test_synthesize_payload_carries_params(settings):
    make_voice(settings)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes(), headers={"X-Duration-Sec": "0.1"})

    engine = engine_with(handler, settings)
    engine.synthesize(
        "你重说一遍！",
        "v_test",
        SynthParams(emo_vector=(0, 0, 0, 0, 0, 0, 0, 0.9), rate=1.05, lang="ZH", pronunciation={"重": "CHONG2"}),
        Path(settings.data_dir) / "a.wav",
    )
    assert captured["refId"] == "ref_1"
    assert captured["emoVector"][7] == 0.9
    assert captured["rate"] == 1.05
    assert captured["lang"] == "ZH"
    assert captured["pronunciation"] == {"重": "CHONG2"}
    assert captured["format"] == "wav"


def test_bad_ref_triggers_reupload_and_retry(settings):
    make_voice(settings)
    state = {"refs": 0, "synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            state["refs"] += 1
            return httpx.Response(200, json={"refId": f"ref_{state['refs']}"})
        state["synth"] += 1
        if state["synth"] == 1:
            return httpx.Response(404, json={"detail": {"code": "bad_ref", "message": "unknown refId"}})
        return httpx.Response(200, content=wav_bytes(), headers={"X-Duration-Sec": "0.1"})

    engine = engine_with(handler, settings)
    engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")
    assert state == {"refs": 2, "synth": 2}


def test_missing_voice_file_raises_voice_missing(settings):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("不应发起请求")

    engine = engine_with(handler, settings)
    with pytest.raises(TtsVoiceMissing):
        engine.synthesize("第一句。", "v_missing", SynthParams(), Path(settings.data_dir) / "a.wav")


def test_service_errors_map_to_typed_exceptions(settings):
    make_voice(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(503, json={"detail": {"code": "oom", "message": "CUDA out of memory"}})

    engine = engine_with(handler, settings)
    with pytest.raises(TtsOom):
        engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")

    def busy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(429, text="slow down")

    with pytest.raises(TtsBusy):
        engine_with(busy, settings).synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")


def test_duration_falls_back_to_wav_header(settings):
    make_voice(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        return httpx.Response(200, content=wav_bytes(seconds=0.5))

    engine = engine_with(handler, settings)
    result = engine.synthesize("第一句。", "v_test", SynthParams(), Path(settings.data_dir) / "a.wav")
    assert result.duration == pytest.approx(0.5, abs=1e-3)
    assert result.sample_rate == 22050
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_http_tts_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audiobook.engines.http_tts'`

- [ ] **Step 3: 写最小实现**

`src/audiobook/engines/http_tts.py`：

```python
import logging
import os
from pathlib import Path

import httpx

from .. import audio, store
from .base import AudioResult, EngineCapabilities, SynthParams
from .errors import TtsBadRef, TtsError, TtsVoiceMissing, classify_status

logger = logging.getLogger(__name__)


class RefCache:
    """进程内 refId 缓存：同一个 (端点, 音色) 只上传一次参考音频。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[float, int, str]] = {}

    def key(self, base_url: str, voice_id: str, path: Path) -> tuple[str, str]:
        stat = path.stat()
        return (base_url, f"{voice_id}:{stat.st_mtime_ns}:{stat.st_size}")

    def get(self, key) -> str | None:
        return self._data.get(key, (0, 0, None))[2]

    def set(self, key, ref_id: str) -> None:
        self._data[key] = (0.0, 0, ref_id)

    def drop(self, key) -> None:
        self._data.pop(key, None)


class HttpTtsEngine:
    """单个 TTS 服务端点的客户端（多端点由 TtsPool 负责）。"""

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
                rate=bool(data.get("rate")),
                pronunciation=bool(data.get("pronunciation")),
                sample_rate=int(data.get("sampleRate") or 22050),
                max_text_chars=int(data.get("maxTextChars") or 300),
            )
        return self._caps

    def synthesize(self, text: str, voice_id: str, params: SynthParams | None, out_path: Path) -> AudioResult:
        params = params or SynthParams()
        ref_id = self._ref_id(voice_id)
        payload = {
            "text": text,
            "refId": ref_id,
            "lang": params.lang or "ZH",
            "emoVector": list(params.emo_vector) if params.emo_vector else None,
            "rate": params.rate,
            "pronunciation": params.pronunciation or None,
            "format": "wav",
        }
        try:
            response = self._post_synthesize(payload)
        except TtsBadRef:
            self._refs.drop(self._ref_key(voice_id))
            payload["refId"] = self._ref_id(voice_id)
            response = self._post_synthesize(payload)
        return self._store_wav(response, out_path)

    # --- 内部 ---

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
        path = store.voice_ref_path(self.settings, voice_id)
        if not path.exists():
            raise TtsVoiceMissing(f"缺少参考音频: {path}")
        key = self._refs.key(self.base_url, voice_id, path)
        cached = self._refs.get(key)
        if cached:
            return cached
        files = {"file": (path.name, path.read_bytes(), "audio/wav")}
        try:
            response = self._client.post(
                "v1/refs",
                files=files,
                data={"refText": ""},
                timeout=self.settings.tts_ref_upload_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TtsError(f"上传参考音频失败: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        ref_id = response.json().get("refId")
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_http_tts_engine.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```powershell
git add src/audiobook/engines tests/test_http_tts_engine.py
git commit -m "feat: 单端点 TTS HTTP 客户端" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 3: 长句分块与拼接

**Files:**
- Create: `src/audiobook/text/chunking.py`
- Modify: `src/audiobook/analysis/characters.py`（改为复用 `text.chunking.chunk_text`）
- Modify: `src/audiobook/engines/http_tts.py`（分块路径）
- Test: `tests/test_http_tts_chunking.py`、`tests/test_pass_a_characters.py`（不变，验证移动后仍可导入）

**Interfaces:**
- Produces: `text.chunking.chunk_text(text, max_chars) -> list[str]`（M1 的实现原样搬过来，拼接后等于原文）；`analysis.characters.chunk_text` 仍可用（重新导出）
- `HttpTtsEngine.synthesize` 新行为：当 `len(text) > limit` 时按 `chunk_text` 切块，逐块合成 → `audio.concat_with_pauses` 拼接（块间 120ms）→ 只保留最终文件；`limit = settings.tts_max_line_chunk_chars or capabilities().max_text_chars`

为什么服务端已有长文本处理还要分块：服务端内部切分是"为了能合成"，而按行分块让我们能对每一块单独重试、单独记账，也避免单行超限时整行失败。

- [ ] **Step 1: 写失败测试**

`tests/test_http_tts_chunking.py`：

```python
from pathlib import Path

import httpx
import pytest

from audiobook import audio
from audiobook.engines.base import SynthParams
from audiobook.engines.http_tts import HttpTtsEngine
from audiobook.text.chunking import chunk_text
from tests_helpers import make_voice, wav_bytes  # 见 Step 3 说明：测试辅助函数放 tests/helpers.py


def _engine(settings, handler, **kwargs) -> HttpTtsEngine:
    return HttpTtsEngine("http://tts.local", settings, transport=httpx.MockTransport(handler), **kwargs)


def test_chunk_text_is_reused_from_analysis_layer():
    assert chunk_text("第一句。第二句。第三句。", 12) == ["第一句。第二句。", "第三句。"]


def test_short_text_uses_single_request(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.2), headers={"X-Duration-Sec": "0.2"})

    settings = settings.model_copy(update={"tts_max_line_chunk_chars": 100})
    engine = _engine(settings, handler)
    result = engine.synthesize("短句。", "v_test", SynthParams(), Path(settings.data_dir) / "out.wav")
    assert calls["synth"] == 1
    assert result.duration == pytest.approx(0.2, abs=1e-3)


def test_long_text_is_split_concatenated_and_cleaned_up(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.2), headers={"X-Duration-Sec": "0.2"})

    settings = settings.model_copy(update={"tts_max_line_chunk_chars": 12})
    out = Path(settings.data_dir) / "out.wav"
    engine = _engine(settings, handler)
    result = engine.synthesize("第一句。第二句。第三句。", "v_test", SynthParams(), out)

    assert calls["synth"] == 2                      # 12 字上限 → 2 块
    assert result.duration == pytest.approx(0.2 + 0.12 + 0.2, abs=1e-3)   # 块间 120ms
    assert audio.wav_duration(out) == pytest.approx(0.52, abs=1e-3)
    leftovers = list(out.parent.glob("*.part*.wav"))
    assert leftovers == []                          # 中间文件必须清理


def test_chunk_limit_falls_back_to_server_max_text_chars(settings):
    make_voice(settings)
    calls = {"synth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refs":
            return httpx.Response(200, json={"refId": "ref_1"})
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"engine": "e", "engineVersion": "1", "sampleRate": 22050, "maxTextChars": 6})
        calls["synth"] += 1
        return httpx.Response(200, content=wav_bytes(0.1), headers={"X-Duration-Sec": "0.1"})

    engine = _engine(settings, handler)             # tts_max_line_chunk_chars 默认 0 → 用服务端 6
    engine.synthesize("第一句。第二句。", "v_test", SynthParams(), Path(settings.data_dir) / "out.wav")
    assert calls["synth"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_http_tts_chunking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audiobook.text.chunking'`

- [ ] **Step 3: 写最小实现**

`tests/helpers.py`（把 Step 1 里用到的两个辅助函数抽出来，供多个测试文件复用）：

```python
import io
import struct
import wave
from pathlib import Path


def wav_bytes(seconds: float = 0.1, rate: int = 22050) -> bytes:
    frames = int(rate * seconds)
    payload = b"".join(struct.pack("<h", int(8000 * ((i % 100) - 50) / 50)) for i in range(frames))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return stream.getvalue()


def make_voice(settings, voice_id: str = "v_test", content: bytes = b"RIFFfake") -> Path:
    path = settings.voices_dir / voice_id / "ref.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
```

（测试文件用 `from helpers import make_voice, wav_bytes` —— pytest 会把 `tests/` 放进 `sys.path`；`tests/test_http_tts_engine.py` 里的同名私有函数改成复用 `helpers`，避免两处漂移。）

`src/audiobook/text/chunking.py`：把 `analysis/characters.py` 里的 `chunk_text` 连同 `SENTENCE_SPLIT` 原样搬过来。

`src/audiobook/analysis/characters.py`：删掉本地实现，改为

```python
from ..text.chunking import chunk_text  # noqa: F401  兼容旧导入路径
```

`src/audiobook/engines/http_tts.py`：把现有 `synthesize` 拆成两层：

```python
    CHUNK_PAUSE_MS = 120

    def synthesize(self, text: str, voice_id: str, params: SynthParams | None, out_path: Path) -> AudioResult:
        out_path = Path(out_path)
        limit = self.settings.tts_max_line_chunk_chars or self.capabilities().max_text_chars
        chunks = chunk_text(text, limit) if limit and len(text) > limit else [text]
        if len(chunks) <= 1:
            return self._synthesize_once(text, voice_id, params, out_path)

        parts: list[tuple[Path, int]] = []
        for position, chunk in enumerate(chunks, start=1):
            part_path = out_path.with_name(f"{out_path.stem}.part{position:02d}.wav")
            self._synthesize_once(chunk, voice_id, params, part_path)
            parts.append((part_path, self.CHUNK_PAUSE_MS))
        try:
            duration = audio.concat_with_pauses(parts, out_path)
        finally:
            for part_path, _ in parts:
                part_path.unlink(missing_ok=True)
        return AudioResult(path=out_path, duration=duration, sample_rate=self.capabilities().sample_rate)

    def _synthesize_once(self, text, voice_id, params, out_path) -> AudioResult:
        ...  # Task 2 里已有的单次实现（refId + POST + 原子落盘）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_http_tts_chunking.py tests/test_http_tts_engine.py tests/test_pass_a_characters.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add src/audiobook tests
git commit -m "feat: 长句分块合成与拼接" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 4: 端点池（并发自报 + 熔断降档）

**Files:**
- Create: `src/audiobook/engines/pool.py`
- Test: `tests/test_tts_pool.py`

**Interfaces:**
- Produces:
  - `EndpointState`（dataclass：`engine`、`base_url`、`ok`、`capacity`、`limit`、`inflight`、`breaker_until`、`success_streak`、`last_error`、`last_health_at`）
  - `TtsPool(endpoints: list[str], settings, engine_factory=None, clock=time.monotonic, sleep=time.sleep)`
  - `.refresh(force=False)`、`.capabilities()`、`.concurrency_hint() -> int`、`.status() -> dict`、`.synthesize(...)`、`.close()`
- 行为契约：
  1. `refresh()` 对每个端点 `GET /health`，读 `recommendedConcurrency` 作为 `capacity`，首次成功时 `limit = capacity`；失败端点 `ok=False` + 记 `last_error`。
  2. `concurrency_hint()` = 所有健康端点的 `(limit - inflight)` 之和；全部不健康 → `0`；至少 1 个健康但都满 → `1`（保证还能串行推进）。
  3. 投递选择：在"健康 + 不在冷却 + inflight < limit"的端点里选 `inflight / limit` 最小的，比值相同按 `base_url` 升序。
  4. `TtsOom` → 该端点 `limit = max(1, limit // 2)`、冷却 `settings.tts_breaker_seconds`（默认 60s）；`TtsBusy` → `limit = max(1, limit - 1)`、短暂冷却 5s；`TtsUnavailable` → `ok=False` + 冷却。
  5. 连续 8 次成功后 `limit += 1`（不超过 `capacity`）。
  6. 所有端点不可用 → `TtsUnavailable`；都满且等到超时 → `TtsBusy`。

- [ ] **Step 1: 写失败测试**

`tests/test_tts_pool.py`：

```python
from pathlib import Path

import pytest

from audiobook.engines.base import AudioResult, EngineCapabilities, SynthParams
from audiobook.engines.errors import TtsBusy, TtsOom, TtsUnavailable
from audiobook.engines.pool import TtsPool


def _caps() -> EngineCapabilities:
    return EngineCapabilities(
        name="fake-tts", version="1", emotions=True, emotion_dims=("happy",),
        rate=True, pronunciation=True, sample_rate=22050, max_text_chars=300,
    )


class FakeEndpoint:
    def __init__(self, capacity: int = 2, error: Exception | None = None, status: str = "ok"):
        self.capacity = capacity
        self.error = error
        self.status = status
        self.calls = 0
        self.health_calls = 0

    def health(self) -> dict:
        self.health_calls += 1
        return {"status": self.status, "recommendedConcurrency": self.capacity, "inflight": 0}

    def capabilities(self) -> EngineCapabilities:
        return _caps()

    def synthesize(self, text, voice_id, params, out_path) -> AudioResult:
        self.calls += 1
        if self.error:
            raise self.error
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")
        return AudioResult(path=Path(out_path), duration=0.1, sample_rate=22050)

    def close(self) -> None:
        pass


def _pool(settings, endpoints, clock=None) -> tuple[TtsPool, list[FakeEndpoint]]:
    fakes = list(endpoints)
    factory = lambda url: fakes.pop(0)  # noqa: E731
    pool = TtsPool(
        [f"http://e{i}.local" for i in range(len(endpoints))],
        settings,
        engine_factory=factory,
        clock=clock or (lambda: 0.0),
    )
    return pool, [state.engine for state in pool.states]


def test_refresh_reads_capacity_and_health(settings):
    pool, endpoints = _pool(settings, [FakeEndpoint(3), FakeEndpoint(1)])
    pool.refresh()
    assert pool.concurrency_hint() == 4
    assert [e.health_calls for e in endpoints] == [1, 1]
    assert pool.capabilities().name == "fake-tts"


def test_oom_downgrades_limit_and_opens_breaker(settings):
    now = {"t": 0.0}
    pool, endpoints = _pool(settings, [FakeEndpoint(4, error=TtsOom("oom")), FakeEndpoint(2)], clock=lambda: now["t"])
    pool.refresh()
    with pytest.raises(TtsOom):
        pool.synthesize("第一句。", "v", SynthParams(), Path(settings.data_dir) / "a.wav")
    state = pool.states[0]
    assert state.limit == 2
    assert state.breaker_until == pytest.approx(60.0)
    # 熔断期内第二次调用必须落到另一个端点
    pool.synthesize("第二句。", "v", SynthParams(), Path(settings.data_dir) / "b.wav")
    assert endpoints[0].calls == 1 and endpoints[1].calls == 1


def test_busy_downgrades_by_one(settings):
    pool, _ = _pool(settings, [FakeEndpoint(4, error=TtsBusy("busy"))])
    pool.refresh()
    with pytest.raises(TtsBusy):
        pool.synthesize("第一句。", "v", SynthParams(), Path(settings.data_dir) / "a.wav")
    assert pool.states[0].limit == 3


def test_successes_restore_limit_gradually(settings):
    pool, _ = _pool(settings, [FakeEndpoint(4)])
    pool.refresh()
    pool.states[0].limit = 2
    for index in range(9):
        pool.synthesize(f"第{index}句。", "v", SynthParams(), Path(settings.data_dir) / f"{index}.wav")
    assert pool.states[0].limit == 3


def test_all_endpoints_down_raises_unavailable(settings):
    pool, _ = _pool(settings, [FakeEndpoint(0, status="unloaded")])
    pool.refresh()
    with pytest.raises(TtsUnavailable):
        pool.synthesize("第一句。", "v", SynthParams(), Path(settings.data_dir) / "a.wav")


def test_load_balancing_prefers_less_loaded_endpoint(settings):
    pool, endpoints = _pool(settings, [FakeEndpoint(1), FakeEndpoint(4)])
    pool.refresh()
    pool.synthesize("第一句。", "v", SynthParams(), Path(settings.data_dir) / "a.wav")
    pool.synthesize("第二句。", "v", SynthParams(), Path(settings.data_dir) / "b.wav")
    assert endpoints[1].calls == 2      # capacity 4 的端点承担更多


def test_status_exposes_endpoint_details(settings):
    pool, _ = _pool(settings, [FakeEndpoint(2)])
    pool.refresh()
    payload = pool.status()
    assert payload["endpoints"][0]["capacity"] == 2
    assert payload["concurrency"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_tts_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audiobook.engines.pool'`

- [ ] **Step 3: 写最小实现**

`src/audiobook/engines/pool.py`：

```python
import logging
import time
from dataclasses import dataclass, field

from .errors import TtsBusy, TtsError, TtsOom, TtsUnavailable
from .http_tts import HttpTtsEngine

logger = logging.getLogger(__name__)

BUSY_COOLDOWN_SECONDS = 5.0
RESTORE_AFTER = 8


@dataclass
class EndpointState:
    engine: object
    base_url: str
    ok: bool = False
    capacity: int = 0
    limit: int = 0
    inflight: int = 0
    breaker_until: float = 0.0
    success_streak: int = 0
    last_error: str | None = None
    last_health_at: float = 0.0
    extra: dict = field(default_factory=dict)


class TtsPool:
    """多端点 TTS 池：并发上限来自服务端自报，5xx/OOM 熔断降档后逐级恢复。"""

    def __init__(self, endpoints, settings, engine_factory=None, clock=time.monotonic, sleep=time.sleep):
        if not endpoints:
            raise TtsUnavailable("未配置任何 TTS 端点")
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        factory = engine_factory or (lambda url: HttpTtsEngine(url, settings))
        self.states = [EndpointState(engine=factory(url), base_url=url.rstrip("/")) for url in endpoints]

    # --- 生命周期 ---

    def close(self) -> None:
        for state in self.states:
            close = getattr(state.engine, "close", None)
            if callable(close):
                close()

    def refresh(self, force: bool = False) -> None:
        now = self._clock()
        for state in self.states:
            if not force and state.last_health_at and now - state.last_health_at < self.settings.tts_health_cache_seconds:
                continue
            try:
                health = state.engine.health()
            except Exception as exc:  # noqa: BLE001 - 任何探测失败都算端点不可用
                state.ok = False
                state.last_error = f"{type(exc).__name__}: {exc}"
                state.last_health_at = now
                continue
            state.last_health_at = now
            state.ok = str(health.get("status") or "").lower() in ("ok", "loading")
            state.capacity = int(health.get("recommendedConcurrency") or 0)
            state.extra = dict(health)
            state.last_error = None
            if state.capacity <= 0:
                state.ok = False
                state.last_error = "recommendedConcurrency=0"
            if state.limit == 0 and state.ok:
                state.limit = state.capacity

    def capabilities(self):
        self.refresh()
        for state in self.states:
            if state.ok:
                return state.engine.capabilities()
        raise TtsUnavailable("没有可用的 TTS 端点")

    def concurrency_hint(self) -> int:
        self.refresh()
        healthy = [state for state in self.states if state.ok]
        if not healthy:
            return 0
        free = sum(max(0, state.limit - state.inflight) for state in healthy)
        return max(1, free)

    def status(self) -> dict:
        return {
            "concurrency": self.concurrency_hint(),
            "endpoints": [
                {
                    "base_url": state.base_url,
                    "ok": state.ok,
                    "capacity": state.capacity,
                    "limit": state.limit,
                    "inflight": state.inflight,
                    "breaker_seconds_left": max(0.0, round(state.breaker_until - self._clock(), 3)),
                    "last_error": state.last_error,
                }
                for state in self.states
            ],
        }

    # --- 投递 ---

    def synthesize(self, text, voice_id, params, out_path):
        state = self._acquire()
        state.inflight += 1
        try:
            result = state.engine.synthesize(text, voice_id, params, out_path)
        except TtsOom as exc:
            self._downgrade(state, factor=0.5, cooldown=self.settings.tts_breaker_seconds, reason=str(exc))
            raise
        except TtsBusy as exc:
            self._downgrade(state, factor=None, cooldown=BUSY_COOLDOWN_SECONDS, reason=str(exc))
            raise
        except TtsUnavailable as exc:
            state.ok = False
            state.last_error = str(exc)
            state.breaker_until = self._clock() + self.settings.tts_breaker_seconds
            raise
        else:
            self._restore(state)
            return result
        finally:
            state.inflight -= 1

    def _acquire(self, timeout: float | None = None) -> EndpointState:
        self.refresh()
        deadline = self._clock() + (timeout or self.settings.tts_timeout_seconds)
        while True:
            healthy = [state for state in self.states if state.ok]
            if not healthy:
                raise TtsUnavailable("所有 TTS 端点均不可用: " + "; ".join(
                    f"{state.base_url}={state.last_error}" for state in self.states
                ))
            now = self._clock()
            ready = [
                state for state in healthy
                if state.breaker_until <= now and state.inflight < max(1, state.limit)
            ]
            if ready:
                ready.sort(key=lambda state: (state.inflight / max(1, state.limit), state.base_url))
                return ready[0]
            if self._clock() >= deadline:
                raise TtsBusy("所有 TTS 端点都在忙或处于冷却期")
            self._sleep(0.05)
            self.refresh(force=True)

    def _downgrade(self, state: EndpointState, *, factor: float | None, cooldown: float, reason: str) -> None:
        state.limit = max(1, int(state.limit * factor)) if factor else max(1, state.limit - 1)
        state.success_streak = 0
        state.breaker_until = self._clock() + cooldown
        state.last_error = reason
        logger.warning("TTS 端点 %s 降档至 %s（冷却 %.0fs）：%s", state.base_url, state.limit, cooldown, reason)

    def _restore(self, state: EndpointState) -> None:
        state.success_streak += 1
        if state.success_streak >= RESTORE_AFTER and state.limit < state.capacity:
            state.limit += 1
            state.success_streak = 0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_tts_pool.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```powershell
git add src/audiobook/engines tests/test_tts_pool.py
git commit -m "feat: TTS 端点池与熔断降档" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 5: 引擎工厂与接线

**Files:**
- Create: `src/audiobook/engines/factory.py`
- Modify: `src/audiobook/handlers/synthesize.py`、`handlers/post.py`、`cli.py`、`api/app.py`
- Modify: `.env.example`
- Test: `tests/test_engine_factory.py`；改 `tests/test_handler_synthesize.py`、`tests/test_handler_post.py`、`tests/test_api.py`

**Interfaces:**
- Produces:
  - `build_engine(settings)`：`engine=fake` → `FakeEngine()`；`engine ∈ {http, tts, indextts}` → `TtsPool(settings.tts_endpoints, settings)`（无端点时抛 `TtsUnavailable`）；其他值 → `ValueError`
  - `effective_concurrency(ctx) -> int`（`handlers/synthesize.py`）：引擎有 `concurrency_hint()` 且返回值 >0 时用 `min(settings.synth_concurrency_max, hint)`，否则回落到 `settings.synth_concurrency`
  - `GET /api/tts/status`：`{"engine": ..., "concurrency": <int>, "endpoints": [...], "error": <str|null>}`
- 行级失败统一走 `record_issue`：`TtsVoiceMissing → tts_ref_missing`、其他 `TtsError → tts_line_failed`；`post` 缺片段 → `audio_missing`（`line` 字段填行 id）
- 整章因端点全挂而失败（`TtsUnavailable`）时补记一条 `tts_endpoint_down` 并抛异常，让队列按 3.4 退避重试

- [ ] **Step 1: 写失败测试**

`tests/test_engine_factory.py`：

```python
import pytest

from audiobook.config import get_settings
from audiobook.engines.errors import TtsUnavailable
from audiobook.engines.fake import FakeEngine
from audiobook.engines.factory import build_engine
from audiobook.engines.pool import TtsPool


def test_build_engine_defaults_to_fake(settings):
    assert isinstance(build_engine(settings), FakeEngine)


def test_build_engine_returns_pool_for_http(settings):
    settings = settings.model_copy(update={"engine": "http", "tts_endpoints": ["http://a.local", "http://b.local"]})
    engine = build_engine(settings)
    assert isinstance(engine, TtsPool)
    assert [state.base_url for state in engine.states] == ["http://a.local", "http://b.local"]
    engine.close()


def test_build_engine_requires_endpoints_for_http(settings):
    with pytest.raises(TtsUnavailable):
        build_engine(settings.model_copy(update={"engine": "http", "tts_endpoints": []}))


def test_build_engine_rejects_unknown_name(settings):
    with pytest.raises(ValueError):
        build_engine(settings.model_copy(update={"engine": "omnivoice"}))
```

`tests/test_handler_synthesize.py` 追加（文件已有 `CountingEngine` 与 `_prepare_book`）：

```python
class HintedEngine(FakeEngine):
    def __init__(self, hint: int):
        super().__init__(ms_per_char=10.0)
        self._hint = hint

    def concurrency_hint(self) -> int:
        return self._hint


class MissingRefEngine(FakeEngine):
    def __init__(self):
        super().__init__(ms_per_char=10.0)

    def synthesize(self, text, voice_id, params, out_path):
        if "第二句" in text:
            raise TtsVoiceMissing("缺少参考音频: data/voices/v_missing/ref.wav")
        return super().synthesize(text, voice_id, params, out_path)


def test_effective_concurrency_prefers_engine_hint(settings):
    ctx = WorkerContext(settings=settings, conn=None, worker_id="w1", engine=HintedEngine(3))
    assert effective_concurrency(ctx) == 3
    capped = settings.model_copy(update={"synth_concurrency_max": 2})
    assert effective_concurrency(WorkerContext(settings=capped, conn=None, worker_id="w1", engine=HintedEngine(3))) == 2
    fallback = WorkerContext(settings=settings, conn=None, worker_id="w1", engine=FakeEngine())
    assert effective_concurrency(fallback) == settings.synth_concurrency


def test_line_failure_is_recorded_with_tts_issue_kind(conn, settings, narrator_lines):
    engine = MissingRefEngine()
    _prepare_book(narrator_lines, settings)
    ctx = WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)
    jobs.enqueue(conn, "synthesize", "b1", 1)
    run_once(ctx)

    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert [issue["kind"] for issue in issues] == ["tts_ref_missing"]
    assert issues[0]["line"] == "c0001-s01-l002"
    assert (store.audio_dir(settings, "b1", 1) / "c0001-s01-l001.wav").exists()
```

`tests/test_handler_post.py` 里 `test_post_skips_missing_clip_and_records_issue` 改成断言新结构：

```python
    issues = store.read_jsonl(store.issues_path(settings, "b1"))
    assert any(row["kind"] == "audio_missing" and row["line"] == "c0001-s01-l002" for row in issues)
```

`tests/test_api.py` 追加：

```python
def test_tts_status_reports_configured_engine(settings):
    client, _ = make_client(settings)
    payload = client.get("/api/tts/status").json()
    assert payload["engine"] == "fake"
    assert payload["concurrency"] == settings.synth_concurrency
    assert payload["endpoints"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_engine_factory.py tests/test_handler_synthesize.py tests/test_handler_post.py tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audiobook.engines.factory'`

- [ ] **Step 3: 写最小实现**

`src/audiobook/engines/factory.py`：

```python
from .errors import TtsUnavailable
from .fake import FakeEngine
from .pool import TtsPool

HTTP_ENGINE_NAMES = {"http", "tts", "indextts", "indextts-2.5"}


def build_engine(settings):
    name = (settings.engine or "fake").strip().lower()
    if name == "fake":
        return FakeEngine()
    if name in HTTP_ENGINE_NAMES:
        if not settings.tts_endpoints:
            raise TtsUnavailable(
                "未配置 TTS 端点：请设置 AB_TTS_ENDPOINTS（例如 http://127.0.0.1:8020）并确认 TTS 服务已启动"
            )
        return TtsPool(settings.tts_endpoints, settings)
    raise ValueError(f"未知引擎: {settings.engine}")
```

`src/audiobook/handlers/synthesize.py`：

```python
from ..analysis.issues import record_issue
from ..engines.errors import TtsError, TtsUnavailable, TtsVoiceMissing


def effective_concurrency(ctx) -> int:
    hint = getattr(ctx.engine, "concurrency_hint", None)
    if callable(hint):
        reported = hint()
        if reported and reported > 0:
            return max(1, min(ctx.settings.synth_concurrency_max, int(reported)))
    return max(1, ctx.settings.synth_concurrency)


@register("synthesize")
def handle_synthesize(ctx, job) -> None:
    ...
    issues: list[dict] = []
    endpoint_down = False
    with ThreadPoolExecutor(max_workers=effective_concurrency(ctx)) as pool:
        ...
            except Exception as exc:
                kind = "tts_line_failed"
                if isinstance(exc, TtsVoiceMissing):
                    kind = "tts_ref_missing"
                if isinstance(exc, TtsUnavailable):
                    endpoint_down = True
                record_issue(
                    ctx.settings, job.book_id, kind,
                    reason=f"{type(exc).__name__}: {exc}",
                    chapter=job.chapter_index, line=row["id"],
                    fallback="该行留空，交由 post 跳过并记 audio_missing",
                    detail={"text": row["text"]},
                )
    if endpoint_down and done == total and not any_clip_ok:
        record_issue(ctx.settings, job.book_id, "tts_endpoint_down", reason="所有 TTS 端点不可用", chapter=job.chapter_index)
        raise RuntimeError("TTS 端点全部不可用，任务退避后重试")
```

（`record_issue` 每行写一次即可，不再直接 `append_jsonl` 那个 M0 结构；`issues` 列表与原本的批量写法删掉。）

`src/audiobook/handlers/post.py`：缺片段时改成

```python
        record_issue(
            settings, book_id, "audio_missing",
            reason="缺少音频片段", chapter=chapter, line=row["id"],
            fallback="跳过该行，字幕与音频同步偏移", detail={"text": row["text"]},
        )
```

`src/audiobook/cli.py`：

- worker：`engine=build_engine(settings)`（替换原来的 `FakeEngine()`）。
- `serve`：启动时探测一次引擎并打印明确提示（spec §7.4）：

```python
        from .engines.factory import build_engine

        try:
            probe = build_engine(settings)
            if hasattr(probe, "refresh"):
                probe.refresh(force=True)
                print(probe.status())
            probe.close() if hasattr(probe, "close") else None
        except Exception as exc:  # noqa: BLE001 - 起服务不因为 TTS 没起而失败
            print(f"警告：TTS 未就绪（{exc}）。分析/合成任务会等待或失败，请先启动 TTS 服务。")
```

`src/audiobook/api/app.py`：新增

```python
    @app.get("/api/tts/status")
    def tts_status():
        from ..engines.factory import build_engine

        try:
            engine = build_engine(settings)
        except Exception as exc:  # noqa: BLE001 - 状态接口不抛错
            return {"engine": settings.engine, "concurrency": 0, "endpoints": [], "error": str(exc)}
        try:
            if hasattr(engine, "status"):
                payload = engine.status()
                return {"engine": settings.engine, "error": None, **payload}
            return {"engine": settings.engine, "concurrency": settings.synth_concurrency, "endpoints": [], "error": None}
        finally:
            close = getattr(engine, "close", None)
            if callable(close):
                close()
```

`.env.example` 追加：

```
# 合成引擎：fake（无 GPU 验证）/ http（连 TTS 服务）
AB_ENGINE=fake
AB_TTS_ENDPOINTS=["http://127.0.0.1:8020"]
AB_SYNTH_CONCURRENCY_MAX=16
AB_TTS_BREAKER_SECONDS=60
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_engine_factory.py tests/test_handler_synthesize.py tests/test_handler_post.py tests/test_api.py tests/test_pipeline_e2e.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add src/audiobook tests .env.example
git commit -m "feat: 引擎工厂与合成接线" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 6: TTS 服务骨架与 HTTP 契约（fake 后端）

**Files:**
- Create: `tts/pyproject.toml`、`tts/README.md`
- Create: `tts/src/aiab_tts/__init__.py`、`config.py`、`schemas.py`、`state.py`、`app.py`、`cli.py`
- Create: `tts/src/aiab_tts/backends/__init__.py`、`base.py`、`fake.py`
- Test: `tts/tests/test_app_contract.py`、`tts/tests/test_backend_fake.py`

**Interfaces:**
- Produces（服务端）：
  - `TtsSettings`（env 前缀 `AIAB_TTS_`）
  - `TtsBackend` 协议：`name` / `version` / `load()` / `unload()` / `is_loaded()` / `capabilities() -> dict` / `recommended_concurrency() -> int` / `synthesize(SynthesisRequest) -> SynthesisResult`
  - `ServiceState(backend, settings)`：`health()` / `capabilities()` / `warmup()` / `unload()` / `add_ref(bytes, ref_text) -> dict` / `synthesize(payload_dict) -> SynthesisResult`
  - `create_app(settings, state=None) -> FastAPI`、`build_state(settings) -> ServiceState`
  - CLI：`uv run --project tts aiab-tts serve --backend fake --port 8020`、`aiab-tts check --url http://127.0.0.1:8020`
- `tts/pyproject.toml`（独立项目，Python ≥3.10，无 torch 依赖；真实后端走 optional extra）：

```toml
[project]
name = "aiab-tts"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "pydantic>=2.7", "pydantic-settings>=2.3", "python-multipart>=0.0.9", "httpx>=0.27"]

# 真实后端不写进依赖：index-tts 不在 PyPI 上，GPU 机器按 docs/tts-deploy.md
# 从官方仓库安装（uv pip install -e <index-tts checkout>），避免 uv 解析阶段就失败。

[project.scripts]
aiab-tts = "aiab_tts.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 1: 写失败测试**

`tts/tests/test_app_contract.py`：

```python
from fastapi.testclient import TestClient

from aiab_tts.app import create_app
from aiab_tts.config import TtsSettings


def _client(**overrides) -> TestClient:
    settings = TtsSettings(backend="fake", data_dir=".pytest-data", **overrides)
    return TestClient(create_app(settings))


def test_health_reports_self_declared_concurrency():
    payload = _client(max_concurrency=3).get("/health").json()
    assert payload["status"] in ("ok", "unloaded")
    assert payload["recommendedConcurrency"] == 3
    assert payload["engine"] == "fake-tts"
    assert payload["inflight"] == 0
    assert "modelLoaded" in payload and "modelSource" in payload


def test_capabilities_match_frozen_contract():
    payload = _client().get("/capabilities").json()
    assert payload["emotions"] is True
    assert payload["emotionDims"] == ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
    assert payload["rate"] is True and payload["rateRange"] == [0.5, 2.0]
    assert payload["pronunciation"] is True
    assert payload["languages"] == ["ZH", "EN", "JP", "ES", "AR"]
    assert payload["sampleRate"] == 22050
    assert payload["maxTextChars"] == 300


def test_ref_upload_then_synthesize_returns_playable_wav():
    client = _client()
    ref = client.post(
        "/v1/refs",
        files={"file": ("ref.wav", b"RIFFfake", "audio/wav")},
        data={"refText": "参考文本"},
    )
    assert ref.status_code == 200
    ref_id = ref.json()["refId"]

    response = client.post(
        "/v1/synthesize",
        json={
            "text": "第一句。",
            "refId": ref_id,
            "lang": "ZH",
            "emoVector": [0, 0, 0, 0, 0, 0, 0, 0.5],
            "rate": 1.0,
            "pronunciation": {"重": "CHONG2"},
            "format": "wav",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert float(response.headers["X-Duration-Sec"]) > 0.05
    assert response.headers["X-Engine"] == "fake-tts"
    assert response.content[:4] == b"RIFF"


def test_synthesize_rejects_unknown_ref_and_empty_text():
    client = _client()
    missing = client.post("/v1/synthesize", json={"text": "第一句。", "refId": "nope", "lang": "ZH"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "bad_ref"

    client.post("/v1/refs", files={"file": ("ref.wav", b"RIFFfake", "audio/wav")})
    empty = client.post("/v1/synthesize", json={"text": "   ", "refId": "ref_1", "lang": "ZH"})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "bad_request"


def test_warmup_and_unload_transition_status():
    client = _client()
    warm = client.post("/warmup").json()
    assert warm["ok"] is True and warm["modelLoaded"] is True
    unloaded = client.post("/unload").json()
    assert unloaded["ok"] is True and unloaded["modelLoaded"] is False
    assert client.get("/health").json()["status"] == "unloaded"
```

`tts/tests/test_backend_fake.py`：

```python
import io
import wave

from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.fake import FakeBackend


def _request(text="第一句。", rate=1.0, seed=7) -> SynthesisRequest:
    return SynthesisRequest(
        text=text, ref_path="ref.wav", ref_text="", lang="ZH",
        emo_vector=None, rate=rate, pronunciation={}, seed=seed,
    )


def _duration(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload)) as fh:
        return fh.getnframes() / fh.getframerate()


def test_duration_scales_with_text_and_rate():
    backend = FakeBackend()
    base = backend.synthesize(_request("第一句。")).duration_sec
    faster = backend.synthesize(_request("第一句。", rate=2.0)).duration_sec
    longer = backend.synthesize(_request("第一句。第二句。")).duration_sec
    assert faster < base < longer
    assert _duration(backend.synthesize(_request()).audio) > 0


def test_same_seed_is_reproducible():
    backend = FakeBackend()
    assert backend.synthesize(_request(seed=3)).audio == backend.synthesize(_request(seed=3)).audio
    assert backend.synthesize(_request(seed=3)).audio != backend.synthesize(_request(seed=4)).audio


def test_oom_and_slow_mode_are_injectable():
    backend = FakeBackend(oom_on={"爆炸"})
    try:
        backend.synthesize(_request("会爆炸的句子"))
    except RuntimeError as exc:
        assert "out of memory" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("应当抛出显存错误")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project tts pytest tts/tests -v`
Expected: FAIL（`tts/` 还没有任何代码，pytest 收集不到或用例导入失败）

- [ ] **Step 3: 写最小实现**

`tts/src/aiab_tts/config.py`：

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class TtsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIAB_TTS_", env_file=".env", extra="ignore")

    backend: str = "fake"                     # fake | indextts
    host: str = "127.0.0.1"
    port: int = 8020
    data_dir: Path = Path("data")             # refs 缓存目录（相对 tts/）
    model_source: str = "local"               # modelscope | huggingface | local
    model_id: str = "IndexTeam/IndexTTS-2.5"
    model_dir: Path = Path("checkpoints")
    hf_endpoint: str = ""
    max_concurrency: int = 0                  # 0 = 用后端推荐值
    device: str = "cuda:0"
    use_bf16: bool = True
    max_text_chars: int = 300
    queue_timeout_seconds: float = 600.0
    allow_download: bool = True
    verify_manifest: bool = True


def get_settings(**overrides) -> TtsSettings:
    return TtsSettings(**overrides)
```

`tts/src/aiab_tts/backends/base.py`：

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    ref_path: Path
    ref_text: str = ""
    lang: str = "ZH"
    emo_vector: tuple[float, ...] | None = None
    rate: float = 1.0
    pronunciation: dict[str, str] = field(default_factory=dict)
    seed: int | None = None


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    duration_sec: float
    sample_rate: int
    engine: str
    engine_version: str
    elapsed_ms: int


class TtsBackend(Protocol):
    name: str
    version: str

    def load(self) -> None: ...
    def unload(self) -> None: ...
    def is_loaded(self) -> bool: ...
    def capabilities(self) -> dict: ...
    def recommended_concurrency(self) -> int: ...
    def synthesize(self, request: SynthesisRequest) -> SynthesisResult: ...
```

`tts/src/aiab_tts/backends/fake.py`（无 GPU 的可播放后端，同时充当契约测试的参照实现）：

```python
import hashlib
import io
import math
import struct
import time
import wave

from .base import SynthesisRequest, SynthesisResult

EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")
MS_PER_CHAR = 60.0


class FakeBackend:
    """不加载任何模型的参照后端：时长可控、可注入 OOM，用于本地端到端验证。"""

    name = "fake-tts"
    version = "fake-1"

    def __init__(self, sample_rate: int = 22050, delay: float = 0.0, oom_on: set[str] | None = None):
        self.sample_rate = sample_rate
        self.delay = delay
        self.oom_on = oom_on or set()
        self._loaded = False

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def recommended_concurrency(self) -> int:
        return 4

    def capabilities(self) -> dict:
        return {
            "engine": self.name,
            "engineVersion": self.version,
            "emotions": True,
            "emotionDims": list(EMOTION_DIMS),
            "rate": True,
            "rateRange": [0.5, 2.0],
            "pronunciation": True,
            "pronunciationStyles": ["pinyin"],
            "languages": ["ZH", "EN", "JP", "ES", "AR"],
            "sampleRate": self.sample_rate,
            "maxTextChars": 300,
            "supportsSeed": True,
            "supportsWarmup": True,
        }

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        started = time.monotonic()
        if any(token in request.text for token in self.oom_on):
            raise RuntimeError("CUDA out of memory")
        if self.delay:
            time.sleep(self.delay)
        rate = max(0.5, min(2.0, request.rate or 1.0))
        duration = max(0.12, len(request.text) * MS_PER_CHAR / 1000.0) / rate
        seed = request.seed if request.seed is not None else 0
        digest = hashlib.sha256(f"{request.text}|{seed}|{request.emo_vector}".encode("utf-8")).digest()
        base_freq = 180.0 + digest[0]
        frames = int(self.sample_rate * duration)
        period = max(1, int(round(self.sample_rate / base_freq)))
        single = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * base_freq * i / self.sample_rate)))
            for i in range(period)
        )
        payload = (single * (frames // period + 1))[: frames * 2]
        stream = io.BytesIO()
        with wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(payload)
        return SynthesisResult(
            audio=stream.getvalue(),
            duration_sec=frames / self.sample_rate,
            sample_rate=self.sample_rate,
            engine=self.name,
            engine_version=self.version,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
```

`tts/src/aiab_tts/state.py`：

```python
import threading
import time
import uuid
import wave
from pathlib import Path

from .backends.base import SynthesisRequest


def wav_duration_seconds(path: Path) -> float:
    """读 WAV 头算时长；不是合法 WAV（测试里的假字节）返回 0.0 而不是抛错。"""
    try:
        with wave.open(str(path)) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except Exception:  # noqa: BLE001 - 参考音频校验交给真实后端
        return 0.0


class ServiceError(Exception):
    """带错误码的服务异常，由 app 统一转成 {"detail": {"code", "message"}}。"""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ServiceState:
    def __init__(self, backend, settings):
        self.backend = backend
        self.settings = settings
        self.started_at = time.time()
        self.refs: dict[str, dict] = {}
        capacity = settings.max_concurrency or backend.recommended_concurrency() or 1
        self.capacity = max(1, int(capacity))
        self._gate = threading.BoundedSemaphore(self.capacity)
        self.inflight = 0
        self.total_audio_sec = 0.0
        self.total_elapsed_ms = 0
        self._lock = threading.Lock()
        self._status = "unloaded"
        self.refs_dir = Path(settings.data_dir) / "refs"

    # --- 生命周期 ---

    def warmup(self) -> dict:
        started = time.monotonic()
        self.backend.load()
        self._status = "ok"
        return {"ok": True, "modelLoaded": self.backend.is_loaded(),
                "elapsedMs": int((time.monotonic() - started) * 1000)}

    def unload(self) -> dict:
        started = time.monotonic()
        self.backend.unload()
        self._status = "unloaded"
        return {"ok": True, "modelLoaded": self.backend.is_loaded(),
                "elapsedMs": int((time.monotonic() - started) * 1000)}

    def ensure_loaded(self) -> None:
        if not self.backend.is_loaded():
            self.warmup()

    def health(self) -> dict:
        average = (self.total_elapsed_ms / 1000.0) / self.total_audio_sec if self.total_audio_sec else 0.0
        return {
            "status": self._status if self.backend.is_loaded() else "unloaded",
            "modelLoaded": self.backend.is_loaded(),
            "device": self.settings.device,
            "vramTotalMB": None,
            "vramUsedMB": None,
            "recommendedConcurrency": self.capacity if self.backend.is_loaded() else 0,
            "inflight": self.inflight,
            "avgInferenceSecPerAudioSec": round(average, 3),
            "engine": self.backend.name,
            "engineVersion": self.backend.version,
            "modelSource": self.settings.model_source,
            "uptimeSec": int(time.time() - self.started_at),
        }

    def capabilities(self) -> dict:
        return self.backend.capabilities()

    # --- 合成 ---

    def add_ref(self, content: bytes, ref_text: str = "") -> dict:
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        ref_id = f"ref_{uuid.uuid4().hex[:12]}"
        path = self.refs_dir / f"{ref_id}.wav"
        path.write_bytes(content)
        self.refs[ref_id] = {"path": path, "refText": ref_text}
        return {
            "refId": ref_id,
            "durationSec": wav_duration_seconds(path),      # 非法 WAV 返回 0.0，不抛错
            "sampleRate": int(self.backend.capabilities().get("sampleRate") or 22050),
        }

    def synthesize(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            raise ServiceError("bad_request", "text 不能为空", 400)
        ref = self.refs.get(payload.get("refId") or "")
        if not ref:
            raise ServiceError("bad_ref", f"未知 refId: {payload.get('refId')}", 404)
        if len(text) > self.backend.capabilities().get("maxTextChars", 300):
            raise ServiceError("bad_request", "文本超过 maxTextChars，请在客户端分块", 400)
        if not self.backend.is_loaded():
            self.ensure_loaded()
        if not self._gate.acquire(timeout=self.settings.queue_timeout_seconds):
            raise ServiceError("busy", "服务繁忙，请退避重试", 503)
        with self._lock:
            self.inflight += 1
        try:
            request = SynthesisRequest(
                text=text,
                ref_path=ref["path"],
                ref_text=ref.get("refText") or "",
                lang=payload.get("lang") or "ZH",
                emo_vector=tuple(payload["emoVector"]) if payload.get("emoVector") else None,
                rate=float(payload.get("rate") or 1.0),
                pronunciation=payload.get("pronunciation") or {},
                seed=payload.get("seed"),
            )
            result = self.backend.synthesize(request)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                raise ServiceError("oom", f"显存不足: {exc}", 503) from exc
            raise ServiceError("engine_error", str(exc), 500) from exc
        finally:
            with self._lock:
                self.inflight -= 1
            self._gate.release()
        with self._lock:
            self.total_audio_sec += result.duration_sec
            self.total_elapsed_ms += result.elapsed_ms
        return result
```

`tts/src/aiab_tts/app.py`：

```python
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .backends.fake import FakeBackend
from .config import TtsSettings
from .state import ServiceError, ServiceState


def build_backend(settings: TtsSettings):
    backend = (settings.backend or "fake").lower()
    if backend == "fake":
        return FakeBackend()
    if backend in ("indextts", "indextts-2.5"):
        from .backends.indextts import IndexTtsBackend  # 懒加载：只有真后端才 import torch 生态

        return IndexTtsBackend(settings)
    raise ValueError(f"未知后端: {settings.backend}")


def build_state(settings: TtsSettings) -> ServiceState:
    return ServiceState(build_backend(settings), settings)


def create_app(settings: TtsSettings, state: ServiceState | None = None) -> FastAPI:
    app = FastAPI(title="AIAB TTS 服务")
    service = state or build_state(settings)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    def health():
        return service.health()

    @app.get("/capabilities")
    def capabilities():
        return service.capabilities()

    @app.post("/v1/refs")
    async def upload_ref(file: UploadFile = File(...), refText: str = Form("")):
        content = await file.read()
        return service.add_ref(content, refText)

    @app.post("/v1/synthesize")
    def synthesize(payload: dict):
        result = service.synthesize(payload)
        return Response(
            content=result.audio,
            media_type="audio/wav",
            headers={
                "X-Engine": result.engine,
                "X-Engine-Version": result.engine_version,
                "X-Duration-Sec": f"{result.duration_sec:.3f}",
                "X-Sample-Rate": str(result.sample_rate),
                "X-Elapsed-Ms": str(result.elapsed_ms),
            },
        )

    @app.post("/warmup")
    def warmup():
        return service.warmup()

    @app.post("/unload")
    def unload():
        return service.unload()

    return app
```

`tts/src/aiab_tts/cli.py`：`serve`（uvicorn.run(create_app(settings), host, port)）与 `check`（httpx 打 `/health` + `/capabilities`，打印结果，连接失败返回码 2）。

`tts/README.md`：契约摘要 + 环境变量表 + 三个启动命令。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project tts pytest tts/tests -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```powershell
git add tts
git commit -m "feat: TTS 服务骨架与 HTTP 契约" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 7: 服务端并发与显存保护

**Files:**
- Modify: `tts/src/aiab_tts/state.py`、`tts/src/aiab_tts/cli.py`
- Test: `tts/tests/test_state_concurrency.py`

**Interfaces:**
- `state.gpu_info(settings) -> dict`：有 torch 时返回 `{"device": ..., "vramTotalMB": ..., "vramUsedMB": ...}`，无 torch 返回 `{"device": settings.device, "vramTotalMB": None, "vramUsedMB": None}`
- `ServiceState.health()` 用 `gpu_info` 填充显存字段；`unload()` 之后 `recommendedConcurrency == 0`
- `ServiceState.synthesize` 行为：并发门（`max_concurrency` 或后端推荐值）→ 未加载自动 warmup → `RuntimeError("…out of memory…")` 映射成 `oom`；等待超过 `queue_timeout_seconds` 映射成 `busy`
- CLI 新增 `aiab-tts unload --url <base>`（对运行中的服务发 `POST /unload`）

- [ ] **Step 1: 写失败测试**

`tts/tests/test_state_concurrency.py`：

```python
import threading
import time

import pytest

from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.fake import FakeBackend
from aiab_tts.config import TtsSettings
from aiab_tts.state import ServiceError, ServiceState, gpu_info


class SlowBackend(FakeBackend):
    def __init__(self, delay: float = 0.2):
        super().__init__(delay=delay)
        self.max_inflight = 0
        self._active = 0
        self._lock = threading.Lock()

    def synthesize(self, request):
        with self._lock:
            self._active += 1
            self.max_inflight = max(self.max_inflight, self._active)
        try:
            return super().synthesize(request)
        finally:
            with self._lock:
                self._active -= 1


def _state(backend=None, **overrides) -> ServiceState:
    settings = TtsSettings(backend="fake", data_dir=".pytest-data", **overrides)
    return ServiceState(backend or FakeBackend(), settings)


def _prepare(state: ServiceState, text: str = "第一句。") -> str:
    ref = state.add_ref(b"RIFFfake", "参考")
    return ref["refId"]


def test_health_reports_configured_capacity_and_gpu_info():
    state = _state(max_concurrency=3)
    state.warmup()
    health = state.health()
    assert health["recommendedConcurrency"] == 3
    assert health["modelLoaded"] is True
    assert "vramTotalMB" in health
    assert set(gpu_info(TtsSettings())) >= {"device", "vramTotalMB", "vramUsedMB"}


def test_concurrency_gate_serializes_when_capacity_is_one():
    backend = SlowBackend(delay=0.2)
    state = _state(backend, max_concurrency=1)
    state.warmup()
    ref_id = _prepare(state)

    def run():
        state.synthesize({"text": "第一句。", "refId": ref_id, "lang": "ZH"})

    started = time.monotonic()
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert time.monotonic() - started >= 0.35
    assert backend.max_inflight == 1


def test_oom_is_mapped_to_service_error():
    state = _state(FakeBackend(oom_on={"爆炸"}), max_concurrency=1)
    state.warmup()
    ref_id = _prepare(state)
    with pytest.raises(ServiceError) as excinfo:
        state.synthesize({"text": "会爆炸的句子", "refId": ref_id})
    assert excinfo.value.code == "oom"
    assert excinfo.value.status_code == 503


def test_unload_then_synthesize_reloads_automatically():
    state = _state(max_concurrency=2)
    state.warmup()
    ref_id = _prepare(state)
    state.unload()
    assert state.health()["status"] == "unloaded"
    assert state.health()["recommendedConcurrency"] == 0
    result = state.synthesize({"text": "第一句。", "refId": ref_id})
    assert result.duration_sec > 0
    assert state.health()["modelLoaded"] is True


def test_average_inference_ratio_is_tracked():
    state = _state(SlowBackend(delay=0.05), max_concurrency=1)
    state.warmup()
    ref_id = _prepare(state)
    state.synthesize({"text": "第一句。", "refId": ref_id})
    assert state.health()["avgInferenceSecPerAudioSec"] > 0


def test_busy_when_queue_timeout_is_exceeded():
    state = _state(SlowBackend(delay=0.4), max_concurrency=1, queue_timeout_seconds=0.05)
    state.warmup()
    ref_id = _prepare(state)
    started = threading.Event()

    def hold():
        started.set()
        state.synthesize({"text": "第一句。", "refId": ref_id})

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(timeout=1.0)
    time.sleep(0.05)
    with pytest.raises(ServiceError) as excinfo:
        state.synthesize({"text": "第二句。", "refId": ref_id})
    assert excinfo.value.code == "busy"
    holder.join()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project tts pytest tts/tests/test_state_concurrency.py -v`
Expected: FAIL — `ImportError: cannot import name 'gpu_info'`

- [ ] **Step 3: 写最小实现**

`tts/src/aiab_tts/state.py` 追加/修改：

```python
def gpu_info(settings) -> dict:
    info = {"device": settings.device, "vramTotalMB": None, "vramUsedMB": None}
    try:
        import torch
    except ImportError:
        return info
    if not torch.cuda.is_available():
        return info
    try:
        free, total = torch.cuda.mem_get_info()
        info["vramTotalMB"] = int(total / 1024 / 1024)
        info["vramUsedMB"] = int((total - free) / 1024 / 1024)
    except Exception:  # noqa: BLE001 - 拿不到显存不影响服务可用
        pass
    return info


def _release_gpu_memory() -> None:
    import gc

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
```

`ServiceState.health()` 里显存字段改成 `**gpu_info(self.settings)`；`warmup()` 前把 `self._status = "loading"`，成功后 `"ok"`，异常时 `"error"` 并重新抛出；`unload()` 末尾调用 `_release_gpu_memory()`。

`tts/src/aiab_tts/cli.py`：新增 `unload` 子命令（httpx.post(f"{url}/unload")，连接失败返回码 2）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project tts pytest tts/tests -v`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add tts
git commit -m "feat: TTS 服务并发门与显存保护" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 8: 模型来源三选一 + 权重校验 + 断点续传

**Files:**
- Create: `tts/src/aiab_tts/download.py`
- Modify: `tts/src/aiab_tts/cli.py`（新增 `download` 子命令）
- Test: `tts/tests/test_download.py`

**Interfaces:**
- Produces:
  - `MANIFEST_NAME = "manifest.json"`、`load_manifest(directory) -> dict[str, dict]`（`{相对路径: {"size": int, "sha256": str}}`）
  - `sha256_file(path) -> str`、`verify_files(directory, manifest) -> dict`（`{"ok": [...], "missing": [...], "mismatch": [...]}`）
  - `ModelIntegrityError(RuntimeError)`
  - `fetch_modelscope(model_id, target_dir, revision=None) -> Path`（懒加载 `modelscope.hub.snapshot_download`）
  - `fetch_huggingface(model_id, target_dir, hf_endpoint="") -> Path`（懒加载 `huggingface_hub.snapshot_download`，`hf_endpoint` 写进 `HF_ENDPOINT`）
  - `ensure_model(settings, fetcher=None) -> dict`（返回 `{"path": Path, "source": str, "verified": bool, "warnings": [...]}`）
- 行为契约：
  1. `local`：不下载，只校验；`settings.verify_manifest` 为真且存在 `manifest.json` 时，主权重缺失/哈希不符 → `ModelIntegrityError`。
  2. `modelscope` / `huggingface`：先校验已存在的文件，全部合格则**跳过下载**（断点续传 + 幂等）；否则调用 fetcher（可注入，测试用本地目录拷贝）后再校验。
  3. 辅助模型（w2v-bert-2.0、MaskGCT 语义编解码器、CAMPPlus、BigVGAN）如果写进 manifest 就一起校验；缺失只进 `warnings`（IndexTTS 首次推理会自己拉取），但会在 CLI 输出里明确提示。

- [ ] **Step 1: 写失败测试**

`tts/tests/test_download.py`：

```python
import hashlib
import json
from pathlib import Path

import pytest

from aiab_tts.config import TtsSettings
from aiab_tts.download import (
    ModelIntegrityError,
    ensure_model,
    load_manifest,
    sha256_file,
    verify_files,
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _manifest_for(directory: Path, names: list[str]) -> dict:
    return {
        name: {"size": (directory / name).stat().st_size, "sha256": sha256_file(directory / name)}
        for name in names
    }


def test_verify_files_reports_ok_missing_and_mismatch(tmp_path):
    _write(tmp_path / "a.bin", b"aaa")
    _write(tmp_path / "b.bin", b"bbb")
    manifest = {
        "a.bin": {"size": 3, "sha256": hashlib.sha256(b"aaa").hexdigest()},
        "b.bin": {"size": 3, "sha256": hashlib.sha256(b"wrong").hexdigest()},
        "c.bin": {"size": 3, "sha256": hashlib.sha256(b"ccc").hexdigest()},
    }
    report = verify_files(tmp_path, manifest)
    assert report == {"ok": ["a.bin"], "missing": ["c.bin"], "mismatch": ["b.bin"]}


def test_local_source_verifies_without_downloading(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "config.yaml", b"cfg")
    _write(model_dir / "gpt.pth", b"weights")
    (model_dir / "manifest.json").write_text(
        json.dumps(_manifest_for(model_dir, ["config.yaml", "gpt.pth"])), encoding="utf-8"
    )
    settings = TtsSettings(backend="fake", model_source="local", model_dir=model_dir)
    result = ensure_model(settings)
    assert result["verified"] is True
    assert result["source"] == "local"


def test_local_source_raises_on_tampered_weight(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "gpt.pth", b"weights")
    (model_dir / "manifest.json").write_text(
        json.dumps(_manifest_for(model_dir, ["gpt.pth"])), encoding="utf-8"
    )
    _write(model_dir / "gpt.pth", b"tampered")
    settings = TtsSettings(model_source="local", model_dir=model_dir)
    with pytest.raises(ModelIntegrityError):
        ensure_model(settings)


def test_hub_source_uses_fetcher_and_skips_when_complete(tmp_path):
    hub = tmp_path / "hub"
    _write(hub / "config.yaml", b"cfg")
    _write(hub / "gpt.pth", b"weights")
    target = tmp_path / "checkpoints"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps({name: {"size": (hub / name).stat().st_size, "sha256": sha256_file(hub / name)}
                    for name in ("config.yaml", "gpt.pth")}),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def fetcher(model_id: str, target_dir: Path) -> Path:
        calls["n"] += 1
        for name in ("config.yaml", "gpt.pth"):
            _write(target_dir / name, (hub / name).read_bytes())
        return target_dir

    settings = TtsSettings(model_source="modelscope", model_dir=target)
    first = ensure_model(settings, fetcher=fetcher)
    assert calls["n"] == 1 and first["verified"] is True
    second = ensure_model(settings, fetcher=fetcher)
    assert calls["n"] == 1                     # 已完整 → 不再下载
    assert second["verified"] is True


def test_auxiliary_model_gap_is_a_warning_not_an_error(tmp_path):
    model_dir = tmp_path / "checkpoints"
    _write(model_dir / "gpt.pth", b"weights")
    manifest = _manifest_for(model_dir, ["gpt.pth"])
    manifest["hf_cache/models--facebook--w2v-bert-2.0/pytorch_model.bin"] = {"size": 1, "sha256": "x" * 64}
    (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    settings = TtsSettings(model_source="local", model_dir=model_dir)
    result = ensure_model(settings)
    assert result["verified"] is True
    assert any("w2v-bert" in warning for warning in result["warnings"])
    assert load_manifest(model_dir)["gpt.pth"]["size"] == 7
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project tts pytest tts/tests/test_download.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiab_tts.download'`

- [ ] **Step 3: 写最小实现**

`tts/src/aiab_tts/download.py`：

```python
import hashlib
import json
import os
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MAIN_WEIGHT_HINTS = ("config.yaml", "gpt", "s2mel", "bigvgan", "feat1", "feat2")


class ModelIntegrityError(RuntimeError):
    """权重缺失或哈希不符。"""


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(directory: Path) -> dict[str, dict]:
    path = Path(directory) / MANIFEST_NAME
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def verify_files(directory: Path, manifest: dict[str, dict]) -> dict:
    report = {"ok": [], "missing": [], "mismatch": []}
    for name, expected in sorted(manifest.items()):
        path = Path(directory) / name
        if not path.exists():
            report["missing"].append(name)
            continue
        if expected.get("size") is not None and path.stat().st_size != expected["size"]:
            report["mismatch"].append(name)
            continue
        if expected.get("sha256") and sha256_file(path) != expected["sha256"]:
            report["mismatch"].append(name)
            continue
        report["ok"].append(name)
    return report


def fetch_modelscope(model_id: str, target_dir: Path, revision: str | None = None) -> Path:
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError as exc:  # pragma: no cover - 只在真实下载时需要
        raise RuntimeError("未安装 modelscope：请先 uv pip install modelscope，或改用 local 来源") from exc
    snapshot_download(model_id, local_dir=str(target_dir), revision=revision)
    return Path(target_dir)


def fetch_huggingface(model_id: str, target_dir: Path, hf_endpoint: str = "") -> Path:
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 huggingface_hub：请先 uv pip install huggingface_hub，或改用 local 来源") from exc
    snapshot_download(repo_id=model_id, local_dir=str(target_dir))
    return Path(target_dir)


def ensure_model(settings, fetcher=None) -> dict:
    target = Path(settings.model_dir)
    target.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(target)
    report = verify_files(target, manifest) if manifest else {"ok": [], "missing": [], "mismatch": []}
    complete = bool(manifest) and not report["missing"] and not report["mismatch"]
    source = (settings.model_source or "local").lower()

    if source == "local":
        if manifest and not complete and settings.verify_manifest:
            raise ModelIntegrityError(f"本地模型校验失败: missing={report['missing']} mismatch={report['mismatch']}")
        return {"path": target, "source": source, "verified": bool(manifest) and complete, "warnings": _aux_warnings(report)}

    if not complete:
        if not settings.allow_download:
            raise ModelIntegrityError("allow_download=false 但模型不完整")
        if fetcher is None:
            fetcher = (
                (lambda model_id, directory: fetch_modelscope(model_id, directory))
                if source == "modelscope"
                else (lambda model_id, directory: fetch_huggingface(model_id, directory, settings.hf_endpoint))
            )
        fetcher(settings.model_id, target)
        report = verify_files(target, manifest) if manifest else verify_files(target, {})
        if manifest and (report["missing"] or report["mismatch"]):
            raise ModelIntegrityError(f"下载后校验失败: missing={report['missing']} mismatch={report['mismatch']}")
    return {"path": target, "source": source, "verified": True, "warnings": _aux_warnings(report)}


def _aux_warnings(report: dict) -> list[str]:
    return [f"辅助模型未就绪: {name}" for name in report.get("missing", []) if not any(
        hint in name for hint in MAIN_WEIGHT_HINTS
    )]
```

`tts/src/aiab_tts/cli.py`：新增 `download` 子命令（打印 `path` / `source` / `verified`，并把 `warnings` 逐条打印）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project tts pytest tts/tests/test_download.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```powershell
git add tts
git commit -m "feat: 模型来源三选一与权重校验" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 9: IndexTTS-2.5 后端

**Files:**
- Create: `tts/src/aiab_tts/backends/indextts.py`
- Modify: `tts/src/aiab_tts/backends/__init__.py`
- Test: `tts/tests/test_backend_indextts.py`

**Interfaces:**
- Produces:
  - `apply_pronunciation(text: str, mapping: dict[str, str]) -> str`：把 `{"行": "XING2"}` 注入成 `银<行|XING2>里` 的 IndexTTS 内联写法；**长词优先**，避免"重"抢先替换"重复"；空表返回原文
  - `IndexTtsBackend(settings)`：`name="indextts-2.5"`、`version="2.5.0"`
    - `load()`：先 `_assert_python(sys.version_info)`（IndexTTS-2.5 只支持 3.10–3.11），再 `from indextts.infer_v2_5 import IndexTTS2` 并 `IndexTTS2(cfg_path=..., model_dir=..., use_bf16=...)`
    - `synthesize(request)`：`text = apply_pronunciation(...)`；`duration_factor = 1 / rate`；调 `self._tts.infer(spk_audio_prompt=..., text=..., lang=..., output_path=..., emo_vector=list(...) if ... else None, duration_factor=...)`；读回 WAV 字节与时长
    - `recommended_concurrency()`：显存 ≥16GB → 3；≥10GB → 2；其余 → 1（`max_concurrency` 配置优先）
    - `unload()`：释放模型引用 + `_release_gpu_memory()`
  - `_assert_python(version_info) -> None`（抛 `RuntimeError`，消息里直接给出 `uv python install 3.11` 的建议）

- [ ] **Step 1: 写失败测试**

`tts/tests/test_backend_indextts.py`：

```python
import io
import sys
import types
import wave

import pytest

from aiab_tts.backends.base import SynthesisRequest
from aiab_tts.backends.indextts import IndexTtsBackend, apply_pronunciation, _assert_python
from aiab_tts.config import TtsSettings


def _fake_wav(path, seconds: float = 0.25, rate: int = 22050) -> None:
    frames = int(rate * seconds)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    with open(path, "wb") as handle:
        handle.write(stream.getvalue())


@pytest.fixture()
def fake_indextts(monkeypatch):
    calls: dict = {}

    class FakeIndexTTS2:
        def __init__(self, cfg_path=None, model_dir=None, use_bf16=True):
            calls["init"] = {"cfg_path": cfg_path, "model_dir": model_dir, "use_bf16": use_bf16}

        def infer(self, **kwargs):
            calls["infer"] = kwargs
            _fake_wav(kwargs["output_path"])
            return kwargs["output_path"]

    module = types.ModuleType("indextts.infer_v2_5")
    module.IndexTTS2 = FakeIndexTTS2
    package = types.ModuleType("indextts")
    package.infer_v2_5 = module
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", module)
    return calls


def test_apply_pronunciation_prefers_longest_word():
    mapping = {"重": "CHONG2", "重复": "CHONG2FU4"}
    assert apply_pronunciation("重复一次，重来。", mapping) == "<重复|CHONG2FU4>一次，<重|CHONG2>来。"
    assert apply_pronunciation("没事", {}) == "没事"


def test_python_guard_rejects_313_with_actionable_message():
    _assert_python((3, 11))
    with pytest.raises(RuntimeError) as excinfo:
        _assert_python((3, 13))
    assert "3.10" in str(excinfo.value) and "uv python install 3.11" in str(excinfo.value)


def test_load_passes_paths_and_bf16(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path, use_bf16=False)
    backend = IndexTtsBackend(settings)
    backend.load()
    assert fake_indextts["init"]["model_dir"] == str(tmp_path)
    assert fake_indextts["init"]["use_bf16"] is False
    assert backend.is_loaded() is True


def test_synthesize_maps_rate_to_duration_factor_and_passes_emotions(fake_indextts, tmp_path):
    settings = TtsSettings(backend="indextts", model_dir=tmp_path)
    backend = IndexTtsBackend(settings)
    backend.load()
    request = SynthesisRequest(
        text="银<行|XING2>里", ref_path=tmp_path / "ref.wav", ref_text="参考",
        lang="ZH", emo_vector=(0, 0, 0, 0, 0, 0, 0, 0.8), rate=1.25,
        pronunciation={"行": "XING2"}, seed=11,
    )
    result = backend.synthesize(request)
    assert fake_indextts["infer"]["duration_factor"] == pytest.approx(0.8)
    assert fake_indextts["infer"]["emo_vector"] == [0, 0, 0, 0, 0, 0, 0, 0.8]
    assert fake_indextts["infer"]["lang"] == "ZH"
    assert fake_indextts["infer"]["spk_audio_prompt"] == str(tmp_path / "ref.wav")
    assert result.duration_sec == pytest.approx(0.25, abs=1e-3)
    assert result.audio[:4] == b"RIFF"
    assert result.engine == "indextts-2.5"


def test_recommended_concurrency_prefers_explicit_setting(tmp_path, monkeypatch):
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=2))
    assert backend.recommended_concurrency() == 2

    auto = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path, max_concurrency=0))
    monkeypatch.setattr("aiab_tts.backends.indextts._vram_total_mb", lambda settings: 24000)
    assert auto.recommended_concurrency() == 3
    monkeypatch.setattr("aiab_tts.backends.indextts._vram_total_mb", lambda settings: 6000)
    assert auto.recommended_concurrency() == 1


def test_unload_releases_model(fake_indextts, tmp_path):
    backend = IndexTtsBackend(TtsSettings(backend="indextts", model_dir=tmp_path))
    backend.load()
    backend.unload()
    assert backend.is_loaded() is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project tts pytest tts/tests/test_backend_indextts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiab_tts.backends.indextts'`

- [ ] **Step 3: 写最小实现**

`tts/src/aiab_tts/backends/indextts.py`：

```python
import logging
import re
import sys
import tempfile
import time
from pathlib import Path

from .base import SynthesisRequest, SynthesisResult

logger = logging.getLogger(__name__)

SUPPORTED_PYTHON = ((3, 10), (3, 11))
EMOTION_DIMS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")


def _assert_python(version_info) -> None:
    if tuple(version_info[:2]) in SUPPORTED_PYTHON:
        return
    raise RuntimeError(
        f"IndexTTS-2.5 需要 Python 3.10 或 3.11，当前是 {version_info[0]}.{version_info[1]}。"
        "请用 `uv python install 3.11` 后在 tts/ 目录执行 `uv sync --python 3.11 --extra indextts`。"
    )


def apply_pronunciation(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text
    result = text
    for word in sorted(mapping, key=len, reverse=True):
        reading = mapping[word]
        if not word or not reading:
            continue
        result = re.sub(re.escape(word), f"<{word}|{reading}>", result)
    return result


def _vram_total_mb(settings) -> int:
    from ..state import gpu_info

    return int(gpu_info(settings).get("vramTotalMB") or 0)


class IndexTtsBackend:
    name = "indextts-2.5"
    version = "2.5.0"

    def __init__(self, settings):
        self.settings = settings
        self._tts = None

    def is_loaded(self) -> bool:
        return self._tts is not None

    def load(self) -> None:
        _assert_python(sys.version_info)
        from indextts.infer_v2_5 import IndexTTS2  # 懒加载：只有真后端才 import

        model_dir = Path(self.settings.model_dir)
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
        duration = _wav_seconds(audio)
        return SynthesisResult(
            audio=audio,
            duration_sec=duration,
            sample_rate=22050,
            engine=self.name,
            engine_version=self.version,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _wav_seconds(payload: bytes) -> float:
    import io
    import wave

    with wave.open(io.BytesIO(payload)) as handle:
        return handle.getnframes() / float(handle.getframerate())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project tts pytest tts/tests -v`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add tts
git commit -m "feat: IndexTTS-2.5 后端适配" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

### Task 10: 契约级端到端 + 部署清单

**Files:**
- Create: `tts/src/aiab_tts/__main__.py`（`python -m aiab_tts serve ...`）
- Create: `tests/test_tts_contract_e2e.py`
- Create: `docs/tts-deploy.md`、`tts/README.md`
- Test: 上述 E2E + 全量 `uv run pytest`

**Interfaces:**
- `tests/test_tts_contract_e2e.py`：用子进程起真实 TTS 服务（fake 后端），后端用 `engine=http` 跑完"导入 → 分析（FakeLLM）→ 合成 → 章节 wav/srt"，并验证 `.meta.json` 里的引擎名是 `fake-tts`
- 服务进程通过 `uv run --project tts python -m aiab_tts serve --backend fake --port <free>` 启动；测试结束必须 `terminate()`
- 环境变量 `AB_SKIP_TTS_E2E=1` 时跳过（用于没网的机器），默认跑

- [ ] **Step 1: 写失败测试**

`tests/test_tts_contract_e2e.py`：

```python
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from audiobook import audio, jobs, store
from audiobook.db import connect, init_db
from audiobook.handlers import casting, characters, lines, post, scenes, split, synthesize  # noqa: F401
from audiobook.importer import import_book
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner
from audiobook.worker import WorkerContext, run_once

REPO = Path(__file__).resolve().parents[1]
TTS_PROJECT = REPO / "tts"

SAMPLE = "第一章 开场\n\n苏锐说：“走。”\n\n王胖子说：“好。”"
PASS_A = {
    "characters": [
        {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年"},
        {"name": "王胖子", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [],
}
PASS_B = {"scenes": [{"index": 1, "title": "开场", "participants": ["苏锐", "王胖子"], "tone": "平静"}]}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(base_url: str, timeout: float = 180.0) -> dict:
    import httpx

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=5.0)
            if response.status_code == 200:
                return response.json()
            last = f"{response.status_code} {response.text[:120]}"
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败是正常的
            last = str(exc)
        time.sleep(0.5)
    raise AssertionError(f"TTS 服务未在 {timeout}s 内就绪：{last}")


def _route_c(user: str) -> dict:
    tail = user.split("句子列表：", 1)[-1]
    sentences = [line.split(". ", 1)[1] for line in tail.splitlines() if ". " in line and line[:1].isdigit()]
    rows = []
    for position, sentence in enumerate(sentences, start=1):
        speaker = "苏锐" if "苏锐" in sentence else ("王胖子" if "王胖子" in sentence else "旁白")
        rows.append({"index": position, "speaker": speaker, "emotion": "平静", "intensity": 0.4})
    return {"lines": rows}


@pytest.mark.skipif(os.environ.get("AB_SKIP_TTS_E2E") == "1", reason="AB_SKIP_TTS_E2E=1")
def test_chapter_synthesis_over_http_service(settings, tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uv", "run", "--project", str(TTS_PROJECT), "python", "-m", "aiab_tts",
         "serve", "--backend", "fake", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        health = _wait_health(base_url)
        assert health["engine"] == "fake-tts"
        assert health["recommendedConcurrency"] >= 1

        settings = settings.model_copy(
            update={"engine": "http", "tts_endpoints": [base_url], "synth_concurrency_max": 4}
        )
        # 音色参考音频（M6 迁移前用最小 WAV 占位即可，fake 后端只校验文件存在）
        ref = settings.voices_dir / "default" / "ref.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"RIFFfake")

        conn = connect(settings.db_path)
        init_db(conn)
        txt = tmp_path / "book.txt"
        txt.write_text(SAMPLE, encoding="utf-8")
        book_id = import_book(settings, conn, txt, title="TTS 契约测试")

        llm = FakeLLM(routes={"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": _route_c})
        from audiobook.engines.factory import build_engine

        ctx = WorkerContext(
            settings=settings, conn=conn, worker_id="w1",
            engine=build_engine(settings),
            llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings),
        )
        guard = 0
        while run_once(ctx):
            guard += 1
            assert guard < 100

        out = store.output_dir(settings, book_id)
        assert (out / "chapter_0000.wav").exists()
        assert audio.wav_duration(out / "chapter_0000.wav") > 0.2
        meta = store.read_json(store.audio_dir(settings, book_id, 0) / "c0000-s01-l001.meta.json")
        assert meta["engine"] == "fake-tts"
        assert all(job.status == "done" for job in jobs.list_jobs(conn, book_id))
        assert not (store.issues_path(settings, book_id)).exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_tts_contract_e2e.py -v`
Expected: FAIL — 服务子进程起不来（`No module named aiab_tts.__main__`）

- [ ] **Step 3: 写最小实现 + 文档**

`tts/src/aiab_tts/__main__.py`：

```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`docs/tts-deploy.md` 内容要点（完整写出，不留 TODO）：

1. **GPU 机器准备**：`uv python install 3.11` → `uv sync --python 3.11 --extra indextts`（`tts/` 目录内）。
2. **模型三选一**：
   - `uv run --project tts aiab-tts download --source modelscope`
   - `uv run --project tts aiab-tts download --source huggingface --hf-endpoint https://hf-mirror.com`
   - 已离线：把权重目录放到 `tts/checkpoints/`，配 `AIAB_TTS_MODEL_SOURCE=local`，`aiab-tts download` 只做校验
3. **manifest 校验**：`manifest.json` 形如 `{"gpt.pth": {"size": 123, "sha256": "..."}}`；辅助模型（w2v-bert-2.0、MaskGCT、CAMPPlus、BigVGAN）同表登记，缺失只告警并提示首次推理会自动拉取。
4. **起服务**：`uv run --project tts aiab-tts serve --backend indextts --host 0.0.0.0 --port 8020`；`aiab-tts check --url http://<host>:8020` 验活。
5. **后端配置**：`.env` 里 `AB_ENGINE=http`、`AB_TTS_ENDPOINTS=["http://<host>:8020"]`；多实例写多个地址，池会自动按各自自报并发分发。
6. **显存共享**：跑别的 GPU 任务前 `aiab-tts unload --url ...` 释放显存，下次合成自动重载。
7. **验收清单**（GPU 机器上照做并记录）：`/health` 的 `recommendedConcurrency`、单章合成耗时、平均 `avgInferenceSecPerAudioSec`、一小时的全书估算、`unload` 前后显存变化。

`tts/README.md`：契约表 + 环境变量表 + 三个常用命令（serve/check/download/unload）+ 一句话说明"后端不 import 本服务，只走 HTTP"。

- [ ] **Step 4: 运行全部测试**

Run: `uv run pytest` 与 `uv run --project tts pytest tts/tests`
Expected: 两个项目都全绿（含刚才的契约级 E2E）

- [ ] **Step 5: 提交**

```powershell
git add tts tests docs
git commit -m "feat: TTS 契约级端到端与部署清单" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

## M2 验收

| 项 | 怎么做 | 现在能不能做 |
|---|---|---|
| B1 后端单元/契约测试 | `uv run pytest` | ✅ 本地 |
| B2 服务端测试 | `uv run --project tts pytest tts/tests` | ✅ 本地（fake 后端） |
| B3 端到端（真实 HTTP 服务 + fake 后端） | `tests/test_tts_contract_e2e.py` | ✅ 本地 |
| B4 真实 IndexTTS-2.5 跑一章 | GPU 机器按 `docs/tts-deploy.md` 起服务，后端 `AB_ENGINE=http` 跑 `aiab run <book>` | ⛔ 需要 GPU（用户机器） |
| B5 改一句只重算一句 | 第 6/10 个任务里的 E2E 断言 + 手工改 `lines/*.jsonl` 一行后重跑 `synthesize` | ✅ 本地 |
| B6 多实例与熔断 | `test_tts_pool.py`（注入 OOM/busy）+ GPU 机器双实例手工验证 | ✅ 单测 / ⛔ 手测 |

## 自检（写计划时已核对）

1. **Spec 覆盖**：§6.1 缓存键（M0 已有）、§6.2 参考音频只上传一次 + 行级并行 + 原子落盘（Task 2、3、5）、§7.1 适配层（Task 2、4、5）、§7.2 六个接口（Task 6、7）、§7.3 三选一 + manifest + 辅助模型（Task 8）、§7.4 多实例/独立启停/serve 探测（Task 4、5）、§3.3 显存 OOM 降档熔断（Task 4、7）。
2. **不做的事**：停顿响度、mkv、整本合本（M3）、UI（M4）、音色迁移（M6）。
3. **类型一致性**：`EngineCapabilities` / `SynthParams` / `AudioResult` 沿用 M0 定义，`HttpTtsEngine` 与 `TtsPool` 都满足 `EngineAdapter`；线上字段（`refId/emoVector/rate/pronunciation/format`）与冻结契约一致。
4. **已知取舍**：`rate` 线上语义是语速倍率，服务端做 `1/rate` 换算（避免后端绑死引擎语义）；长句分块放在客户端（可单独重试与记账）；辅助模型缺失只告警（否则离线机器首次推理永远卡住）。

---

## M2 验收记录（2026-09-24 实跑）

环境：本机 Windows + PowerShell，后端 Python 3.13，TTS 服务用 `--backend fake`（真实 GPU 推理留给部署机器）。

| 项 | 结果 |
|---|---|
| B1 后端测试 | `uv run pytest` → **164 passed** |
| B2 服务端测试 | `uv run --project tts pytest tts/tests` → **27 passed** |
| B3 契约级端到端 | `tests/test_tts_contract_e2e.py`：子进程起真实 TTS 服务 → 后端走 HTTP 跑完整章 → 通过 |
| B4 真实 IndexTTS-2.5 | ⛔ 待 GPU 机器按 `docs/tts-deploy.md` 的 G1–G8 清单执行 |
| B5 全书实跑（HTTP + fake 后端） | 9 章 / 822 片段全部 `done`、0 失败；`audio/**/*.meta.json` 的 `engine=fake-tts`、`engine_version=fake-1`；章节 wav 合计 33.3 分钟；`issues.jsonl` 为空 |
| B6 改一句只重算一句 | 改第 1 章第 2 句后重跑：44 个片段里只有 `c0000-s01-l002.wav` 的 mtime 变化；重拼后 SRT 44 条、末条 `01:58,300 → 01:59,680`、wav 120.13s（时间轴与音频一致） |
| B7 冷启动 | 服务未加载时 `/health` 报 `status=unloaded` + 容量；首次合成自动 warmup，无需人工预热 |

实跑修掉的 3 个真实缺陷（都已补测试）：

1. **健康契约**：服务冷启动时 `recommendedConcurrency=0` 会让后端池把端点判为故障，从而永远触发不了首次加载 —— 改成容量恒报、`status` 如实汇报；池把 `ok|loading|unloaded` 都视为可用。
2. **错误分类**：本地缺参考音频（`TtsVoiceMissing`）原本被池当成"端点不可用"，第一次失败就把整个服务拖下线并刷出 2465 条 `tts_line_failed` —— 现在本地/请求类错误直接上抛，端点保持健康。
3. **空值参数**：后端把 `pronunciation=None` 发给服务端，被 pydantic 拒绝成 `bad_request` —— 客户端改为缺省不下发该字段，服务端也容忍 `null`。

遗留：

1. 音色库仍未迁移（M6），实跑用 `data/voices/default/ref.wav` 占位（取自旧系统的一个真实参考音频；`data/` 不入库）。
2. `rate → duration_factor`、注音内联写法、显存并发估算都只有单元测试与假模块验证，真实音质与显存表现必须在 GPU 机器上按 G1–G8 记录。
3. 第一次失败尝试的异常记录保留在 `data/books/<bookId>/issues.first-attempt.jsonl`，可对照复查。
