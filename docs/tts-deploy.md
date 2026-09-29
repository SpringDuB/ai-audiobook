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

### GPU 版 torch（**Windows 上必做**）

PyPI 上的 `torch` 在 Windows 是 **CPU 版**（`2.8.0+cpu`）。装了它，IndexTTS 会退回 CPU 推理
（日志里那行 "it may take a while to run in CPU mode"），慢几十倍，等于不能用。装 CUDA 版：

```powershell
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 `
  torch==2.8.* torchaudio==2.8.*
uv run --project tts python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 期望看到 2.8.x+cu128 True
```

> 注意：之后**不要再跑 `uv sync`**（它会把 torch 换回 CPU 版）；要同步别的依赖时加 `--inexact`，
> 或者同步完再把 CUDA 版 torch 装回来。

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

### 情绪的两条通道

合成请求里可以带**二选一**的情绪参数（`POST /v1/synthesize`）：

| 字段 | 含义 | 需要什么 |
|---|---|---|
| `emoText` | 一句自然语言描述："压着火气，语速比平时快" | 服务端要加载 QwenEmotion（`AIAB_TTS_USE_QWEN_EMO=1`，多占约 1.2GB 显存） |
| `emoVector` | 8 维情感向量，顺序 happy/angry/sad/afraid/disgusted/melancholic/surprised/calm | 一直可用 |

`/capabilities` 的 `emotionText` 表示服务端是否支持文本通道；不支持时客户端会自动退回 `emoVector`（不会合成失败）。
**当前产品只开放 `emoVector`**：`EMOTION_TEXT_ENABLED = False` 时，`emoText` 通道整条链路（设置页下拉、`AIAB_TTS_USE_QWEN_EMO`、Qwen 加载）都不会启用。
重新开放：把 `src/audiobook/config.py` 的 `EMOTION_TEXT_ENABLED` 改成 `True`，设置页会出现「情绪控制」下拉，`aiab tts start` 会按设置加载 QwenEmotion。

分析侧产出的情绪名是中文（喜悦/愤怒/…），客户端会翻成引擎的英文维度名——少了这步翻译，
`emoVector` 会静默变成 `null`（历史上就踩过这个坑，回归测试在 `tests/test_cache_key.py`）。

服务端并发门 = `min(AIAB_TTS_MAX_CONCURRENCY, 后端安全上限)`：配置只能往下调，顶不过后端自报值。
IndexTTS-2.5 默认 **3 路真并发**。上游的 GPT 推理模型原本把"当前请求的条件嵌入"存在实例属性
`cached_mel_emb` 上，两个请求并发时互相覆盖，会 500
`engine_error: The size of tensor a (N) must match the size of tensor b (M)`；
`tts/src/aiab_tts/indextts_compat.py` 已把它补成线程本地（每个推理线程各存各的），
实测 3 路混合音色、3 条长旁白共 4 轮 12 个请求全部 200，峰值 `inflight=3`。
8G 卡上 3 路会把显存吃满（实测峰值 8187/8187MB），OOM 时池会自动降档并熔断 60 秒。
超过并发门的请求会排队，排队超过 `AIAB_TTS_QUEUE_TIMEOUT_SECONDS` 返回 `busy`；
显存不足时返回 `oom`（后端会据此降档 + 熔断 60 秒）。

### 加载路径（别让权重在主机内存里多住一份）

上游的加载是"CPU 随机初始化 → `torch.load` 整份读进主机内存 → `load_state_dict` → `.to(cuda)`"，
主机里最多同时存在两份权重（`gpt.pth` 单文件 3.1GB），加载完还常常不还给系统。
`tts/src/aiab_tts/indextts_compat.py` 的 `fast_model_loading()` 做了两件事：

1. 大 checkpoint 用 `torch.load(..., mmap=True)` 按页读，不再整份拷进主机内存；
2. GPT / codec / s2mel / campplus 直接在 `cuda:0` 设备上下文里构造，w2v-bert 用
   `device_map` 直进显存，省掉"CPU 一份 + 显存一份"。

同机同模型、全新进程实测：加载期峰值主机内存 **7.04GB → 3.94GB**，加载后提交内存
**12.53GB → 8.34GB**，加载耗时 **24.1s → 18.7s**，显存与产出不变。
出问题可以设 `AIAB_TTS_FAST_LOAD=0` 关掉这条快路。

### 显存：大模块 bf16（默认只降 w2v-bert）

上游只把 GPT 降成了 bf16，w2v-bert（2.2GB fp32）/codec/s2mel/BigVGAN 都还是 fp32。
`apply_bf16_modules()` 默认只降 **w2v-bert**（说话人/语义编码器，对音质最不敏感）：
权重转 bf16，forward 的浮点输入自动跟着转 dtype。

实测（8G 卡）：空闲显存 **5411 → 4837MiB**，3 条最长旁白并发峰值 **8144 → 7030MiB**，
并发耗时 59.4s → 54.4s，请求全部成功。

想再省显存可以显式开启别的模块（**需要自己听一遍**，声码器/flow-matching 更敏感）：

```
AIAB_TTS_BF16_MODULES=w2v,codec,s2mel,campplus     # bigvgan 风险最高，默认不建议
```

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
| `Repo nvidia/bigvgan_v2_22khz_80band_256x not exists` + 掉到 hf-mirror 下载 | 上游 index-tts 的 HF→ModelScope 别名表缺 BigVGAN（ModelScope 上它在 `nv-community/` 命名空间）。我们启动时会自动补上这条别名（`tts/src/aiab_tts/indextts_compat.py`），要拿旧日志里那种慢下载，更新代码后重启 TTS 服务即可 |
