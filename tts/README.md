# aiab-tts（TTS 推理服务）

独立项目：**独立 venv、独立进程、独立部署**。后端（仓库根目录的 `aiab`）只通过 HTTP 调用它，
任何情况下都不会 import 本项目的代码，也不在同一进程里加载模型。

## 快速开始（无 GPU 的本地验证）

```powershell
uv run --project tts aiab-tts serve --backend fake --port 8020
uv run --project tts aiab-tts check --url http://127.0.0.1:8020
```

fake 后端会生成真实可播放的 WAV（时长与文本长度/语速相关），用于跑通整条链路、写测试和验收，
不需要 GPU、不下载模型。

## 真实后端（GPU 机器）

```powershell
uv python install 3.11
uv sync --python 3.11                       # 在 tts/ 目录
uv pip install --python .venv -e <index-tts 仓库路径>   # IndexTTS-2.5 不在 PyPI 上
uv run --project tts aiab-tts download --source modelscope        # 或 huggingface / local
uv run --project tts aiab-tts serve --backend indextts --host 0.0.0.0 --port 8020
```

完整步骤、模型来源三选一、manifest 校验与显存共享见 [`docs/tts-deploy.md`](../docs/tts-deploy.md)。

## HTTP 契约（后端唯一耦合面）

| 接口 | 说明 |
|---|---|
| `GET /health` | `status / modelLoaded / device / vramTotalMB / vramUsedMB / recommendedConcurrency / inflight / avgInferenceSecPerAudioSec / engine / engineVersion / modelSource / uptimeSec` |
| `GET /capabilities` | 情绪维度、语速范围、注音方式、语言、采样率、单次文本上限 |
| `POST /v1/refs` | multipart 上传参考音频一次 → `refId`（服务端缓存） |
| `POST /v1/synthesize` | `text / refId / lang / emoVector / rate / pronunciation / seed / format` → WAV 字节流 + `X-Duration-Sec` 等响应头 |
| `POST /warmup` / `POST /unload` | 预加载 / 释放模型与显存 |

约定：

- `rate` 是**语速倍率**（>1 更快），引擎侧的 `duration_factor = 1 / rate` 由本服务换算。
- `recommendedConcurrency` 是容量（机器/配置决定），**冷启动时也如实上报**；`status` 才是加载状态。
- 错误统一为 `{"detail": {"code": "...", "message": "..."}}`，
  `code ∈ bad_request | bad_ref | busy | oom | engine_error | not_loaded`。
- 文本超过 `maxTextChars` 时由客户端分块（后端已实现），服务端直接拒绝而不是悄悄截断。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIAB_TTS_BACKEND` | `fake` | `fake` / `indextts` |
| `AIAB_TTS_HOST` / `AIAB_TTS_PORT` | `127.0.0.1` / `8020` | 监听地址 |
| `AIAB_TTS_DATA_DIR` | `data` | 参考音频缓存目录 |
| `AIAB_TTS_MODEL_SOURCE` | `local` | `modelscope` / `huggingface` / `local` |
| `AIAB_TTS_MODEL_DIR` | `checkpoints` | 权重目录（含 `manifest.json`） |
| `AIAB_TTS_MODEL_ID` | `IndexTeam/IndexTTS-2.5` | 下载用的仓库名 |
| `AIAB_TTS_HF_ENDPOINT` | 空 | HuggingFace 镜像地址 |
| `AIAB_TTS_MAX_CONCURRENCY` | `0` | 0 = 用后端推荐值（按显存估算） |
| `AIAB_TTS_QUEUE_TIMEOUT_SECONDS` | `600` | 排队超时 → 返回 `busy` |
| `AIAB_TTS_USE_BF16` | `true` | 推理精度 |
| `AIAB_TTS_VERIFY_MANIFEST` | `true` | 启动/下载时校验权重 |

## 命令

| 命令 | 作用 |
|---|---|
| `aiab-tts serve --backend fake\|indextts --host --port` | 起服务 |
| `aiab-tts check --url ...` | 打印健康状态与能力集 |
| `aiab-tts download --source ... [--model-dir] [--hf-endpoint]` | 下载/校验权重（幂等、断点续传） |
| `aiab-tts unload --url ...` | 释放显存（与别的 GPU 任务共享机器时用） |

## 测试

```powershell
uv run --project tts pytest tts/tests
```

27 个用例覆盖：HTTP 契约、并发门与显存保护、fake 后端、IndexTTS 参数映射（含 `rate → duration_factor`、
注音内联写法、Python 版本守卫）、模型来源与 manifest 校验。真实 GPU 推理由 `docs/tts-deploy.md` 的验收清单覆盖。
