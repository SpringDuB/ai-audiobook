# TTS 服务部署与验收（IndexTTS-2.5）

本文覆盖：GPU 机器准备 → 模型三选一 → 起服务 → 后端接线 → 显存共享 → 验收清单。

## 1. 环境准备（GPU 机器）

需要的是一块 NVIDIA GPU 与约 6 GB 显存。

Python 版本：**我们这侧不设门槛**（`tts/pyproject.toml` 只写 `>=3.10`，装得上就能跑），
但 `index-tts` 自己在 `pyproject.toml` 里声明了 `requires-python = ">=3.10,<3.12"`，
所以真后端用 3.11 建 venv 最省事（想用别的版本就得自己处理它的声明与依赖轮子）。

```powershell
cd tts
uv sync --python 3.11 --extra indextts --extra download   # 推理栈 + 下载客户端（版本按 index-tts 对齐）

# IndexTTS 不在 PyPI 上：克隆官方仓库并装进本项目的 venv
git clone https://github.com/index-tts/index-tts.git index-tts
uv pip install --python .venv -e index-tts
```

`indextts` extra = `torch==2.8.* / torchaudio / transformers==4.52.1 / librosa / soundfile / numpy / sentencepiece`
等推理栈（版本按 index-tts 的 pin 对齐，权威来源仍是它的 `pyproject.toml`）；
`download` extra = `modelscope` + `huggingface_hub`。基础依赖（fastapi / uvicorn …）保持轻量，
没 GPU 的机器也能装、能跑单元测试。

> Windows / Linux 上从 PyPI 装的 `torch==2.8.*` 默认就是 CUDA 构建；要用别的 CUDA 版本，
> 先用 PyTorch 官方索引装 torch/torchaudio，再执行上面的 `uv sync`。

> 若某个依赖（例如 `pynini`/`WeTextProcessing`）在你的版本上没有轮子，pip 会要求现场编译；
> 这时要么换成它有轮子的解释器（`uv sync --python 3.11`），要么自行准备编译环境。

## 2. 模型来源三选一

```powershell
# 国内网络直连
uv run --project tts aiab-tts download --source modelscope

# HuggingFace（可走镜像）
uv run --project tts aiab-tts download --source huggingface --hf-endpoint https://hf-mirror.com

# 完全离线：把权重放到 tts/checkpoints/，然后只做校验
uv run --project tts aiab-tts download --source local
```

三种来源最终都落到同一个目录布局（`tts/checkpoints/`），后端配置不需要跟着变。

### manifest 校验

`tts/checkpoints/manifest.json` 记录每个文件的 `size` 与 `sha256`：

```json
{
  "config.yaml": {"size": 1234, "sha256": "..."},
  "gpt.pth": {"size": 987654321, "sha256": "..."},
  "hf_cache/models--facebook--w2v-bert-2.0/pytorch_model.bin": {"size": 123, "sha256": "..."}
}
```

- **主权重**（`config.yaml`、`gpt*`、`s2mel*`、`feat1/2*`、顶层 BigVGAN 等，即不在 `hf_cache/` 下的文件）
  缺失或哈希不符 → 直接报错，不允许带病启动。
- **辅助模型**（w2v-bert-2.0、MaskGCT 语义编解码器、CAMPPlus、BigVGAN 的 hub 缓存，都在 `hf_cache/` 下）
  缺失只打印警告：IndexTTS 首次推理会自行下载。离线机器请提前把它们放进 `hf_cache/` 并写进 manifest，
  否则第一次推理会卡在下载上。
- 已存在且校验通过的文件不会重复下载（断点续传 + 幂等）。

## 3. 起服务（一键启动）

日常使用不用敲命令：浏览器界面 **设置 → TTS 服务** 里选好后端与模型来源，点「一键启动 TTS 服务」。
它会做三件事：

1. 在本机拉起独立进程（直接用 `tts/.venv` 的解释器跑 `-m aiab_tts serve`，绕开 `uv run` 的自动同步——
   正在跑的服务会锁住 venv 里的扩展模块，同步会以 `os error 5` 失败）。日志写到 `data/logs/tts-service.log`，
   界面上可展开实时看；venv 缺失或缺基础依赖时会先自动补一次 `uv sync`（只装基础依赖）；
2. 把合成引擎自动切到 `http` 并指向刚起来的地址（写进 `data/settings.json` 的 `engine` / `tts_endpoints`）；
3. 运行中的 worker **下一轮任务前会重读设置**，所以不用重启 worker 就能用上新服务。

停止用同一页的「停止」（Windows 下按进程树 `taskkill`，不会留孤儿进程）。

命令行等价物：

```powershell
uv run aiab tts start --backend indextts --model-source local --wait 120   # --wait 健康检查最长等的秒数
uv run aiab tts status
uv run aiab tts logs --lines 40
uv run aiab tts stop
```

想手工起、手工接（多实例 / 跨机器）时再走下面两条命令：

```powershell
uv run --project tts aiab-tts serve --backend indextts --host 0.0.0.0 --port 8020
uv run --project tts aiab-tts check --url http://127.0.0.1:8020
```

`check` 会打印：引擎与版本、加载状态、**自报并发**、情绪维度、语速范围、语言、采样率、单次文本上限。

服务端并发门 = `AIAB_TTS_MAX_CONCURRENCY`（0 时按显存估算：≥16 GB → 3，≥10 GB → 2，其余 1）。
超过并发门的请求会排队，排队超过 `AIAB_TTS_QUEUE_TIMEOUT_SECONDS` 返回 `busy`；
显存不足时返回 `oom`（后端会据此降档 + 熔断 60 秒）。

## 4. 后端接线

在仓库根目录的 `.env`：

```
AB_ENGINE=http
AB_TTS_ENDPOINTS=["http://127.0.0.1:8020"]
AB_SYNTH_CONCURRENCY_MAX=16
AB_TTS_BREAKER_SECONDS=60
```

要点：

1. 并发上限来自服务端自报（`recommendedConcurrency`），客户端只保留 `AB_SYNTH_CONCURRENCY_MAX` 作为安全上限。
2. 多实例：`AB_TTS_ENDPOINTS` 写多个地址（可跨机器），池按各端点自报容量分发；某端点 OOM/5xx 会独立降档熔断。
3. 参考音频只在每个 (端点, 音色) 首次上传一次，之后只传 `refId`。
4. 每个音色需要 `data/voices/<voiceId>/ref.wav`（M6 会从旧系统的 96 个音色迁移过来）。
5. `uv run aiab serve` 启动时会探测 TTS 并打印状态；没起服务也不会起不来，只会警告。

## 5. 显存共享

```powershell
uv run --project tts aiab-tts unload --url http://127.0.0.1:8020
```

释放模型与显存缓存（下一次合成请求会自动重新加载）。适合同一台机器上还要跑别的 GPU 任务时使用。

## 6. 验收清单（在 GPU 机器上照做并记录）

| # | 动作 | 记录什么 |
|---|---|---|
| G1 | `aiab-tts check` | `recommendedConcurrency`、`engineVersion` |
| G2 | 后端跑一章：`uv run aiab run <bookId>` + `uv run aiab worker` | 该章 `audio/chapter_XXXX/*.meta.json` 里的 `engine` = `indextts-2.5`、`engine_version` |
| G3 | 观察 `/health` | 推理中的 `inflight` 是否 ≤ `recommendedConcurrency` |
| G4 | 一章跑完看 `/health` 的 `avgInferenceSecPerAudioSec` | 用它估算整本耗时（例：0.4 表示 1 秒音频要 0.4 秒算） |
| G5 | 手工改 `analysis/lines/chapter_XXXX.jsonl` 一行文本后 `uv run aiab run <bookId>` | 只有那一行重新合成（`.meta.json` 时间戳变化，其他不变） |
| G6 | `aiab-tts unload` 前后对比 `vramUsedMB` | 显存是否释放 |
| G7 | 双实例（两个端口/两台机器） | 后端池是否按容量分发（`/api/tts/status` 的 `served` 计数） |
| G8 | 触发一次 OOM（例如把并发调到 8） | 是否降档 + 熔断 60 秒，任务是否退避重试而不是崩掉 |

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `LLM 不可用` / `TTS 服务不可用` | 后端 `.env` 的端点写错，或服务没起；`aiab-tts check` 先验活 |
| `IndexTTS 不可用：No module named 'indextts'` | tts venv 里还没装 index-tts；按第 1 节 `uv pip install -e third_party/index-tts` |
| `not_loaded` | 模型文件缺失/校验失败；`aiab-tts download --source local` 看缺哪些文件 |
| `oom` | 降 `AIAB_TTS_MAX_CONCURRENCY`，或先 `unload` 别的 GPU 任务 |
| `busy` | 并发打满；后端会自动降档重试，持续出现说明该加实例 |
| 首次推理卡住 | 辅助模型在下载；离线机器请提前放置 `hf_cache/` 并写进 manifest |
