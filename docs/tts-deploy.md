# TTS 服务部署与验收

本项目有两个可选的合成后端：

| 后端 | 怎么发声 | venv | 状态 |
|---|---|---|---|
| **qwen3**（默认） | 逐句按"音色描述"生成（Qwen3-TTS VoiceDesign），参考音频克隆备用 | `tts/.venv-qwen`（Python 3.12） | 主线 |
| indextts | 参考音频零样本克隆 + 8 维情绪向量（IndexTTS-2.5） | `tts/.venv`（Python 3.11） | 保留可选 |

---

## 0. qwen3 后端（主线）

```powershell
cd tts
uv sync                                                    # 服务框架（fastapi/uvicorn/pydantic/httpx）
powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1
```

脚本做的事、以及为什么这么做：

1. **独立 venv `tts/.venv-qwen`（Python 3.12）**：qwen-tts 钉 `transformers==4.57.3`，
   而 index-tts 钉 `4.52.1 / py<3.12`，两套塞一个 venv 会互相拆台；主项目按后端自动选 venv
   （见 `src/audiobook/tts_service.py` 的 `venv_python(backend)`）。
2. **CUDA 版 torch**：Windows 上 PyPI 的 torch 是 CPU 版，脚本用
   `--find-links https://mirrors.aliyun.com/pytorch-wheels/cu128/` 装 `torch==2.8.0+cu128`；
   自查：`tts/.venv-qwen/Scripts/python.exe -c "import torch;print(torch.__version__, torch.cuda.is_available())"`
   期望 `2.8.0+cu128 True`。
3. **权重**：`modelscope download` 到 `tts/checkpoints/`：
   - `Qwen3-TTS-12Hz-1.7B-VoiceDesign`（3.8 GB）—— 按描述生成，主力；
   - `Qwen3-TTS-12Hz-1.7B-Base`（3.9 GB）—— 参考音频克隆，备用通道；
   两个都下是因为"手工给角色绑库存音色"那条路要用 Base；只用描述可以不装 Base。

起服务与自查：

```powershell
uv run --project tts aiab-tts serve --backend qwen3 --port 8020
uv run --project tts aiab-tts check --url http://127.0.0.1:8020
# 期望：engine=qwen3-tts 按描述生成=True 音色设计=True
curl http://127.0.0.1:8020/v1/design -H "Content-Type: application/json" `
  -d '{"text":"你先坐下，慢慢说。","instruct":"三十多岁的男性，嗓音低沉略带沙哑，语速偏慢。","lang":"ZH"}' -o design.wav
```

**验收清单（qwen3）**

| 项 | 期望 |
|---|---|
| 加载 | 日志 `Qwen3-TTS design 加载完成`，用时约 6 s；`/health` `vramUsedMB ≈ 4000–4500` |
| 试听 | `POST /v1/design` 返回的 WAV 可播放，采样率 24000 |
| 逐句 | `POST /v1/synthesize` 带 `voicePrompt`、不带 `refId` → 返回 WAV；`instruct` 变了音频也变 |
| 批量 | `POST /v1/synthesize_batch` 4 条一包，`X-Elapsed-Ms` 约为 4 条单发之和的 1/2；显存峰值 < 6 GB |
| 换模型 | 手工绑库存音色后合成：日志出现 `已卸载 design 模型并归还显存` + `加载 Qwen3-TTS base`，显存不超 8 GB |
| 卸载 | `POST /unload` 后 `/health` `modelLoaded=false`、`vramUsedMB` 回到桌面占用 |

主项目侧：设置页把「推理后端」选成 `qwen3`，点「一键启动 TTS 服务」；`AB_TTS_BACKEND=qwen3` 同理。

---

## IndexTTS-2.5 后端（保留）

本文以下部分覆盖：GPU 机器准备 → 模型三选一 → 起服务 → 后端接线 → 显存共享 → 验收清单。

## 1. 环境准备（GPU 机器）

需要的是一块 NVIDIA GPU 与约 6 GB 显存。

Python 版本：**我们这侧不设门槛**（`tts/pyproject.toml` 只写 `>=3.10`，装得上就能跑），
但 `index-tts` 自己在 `pyproject.toml` 里声明了 `requires-python = ">=3.10,<3.12"`，
所以真后端用 3.11 建 venv 最省事（想用别的版本就得自己处理它的声明与依赖轮子）。

```powershell
cd tts
uv sync --python 3.11 --extra indextts --extra download   # 推理栈 + 下载客户端（版本按 index-tts 对齐）

# IndexTTS 不在 PyPI 上：仓库自带 tts/index-tts（上游副本 + 我们的改动），直接装进 venv
uv pip install --python .venv -e index-tts
```

`tts/index-tts/` 已经**随仓库一起管理**（不再 gitignore，也不需要自己 clone）。我们的改动集中在
`indextts/infer_v2_5.py`，全部带 `[AIAB 改动]` 注释：按音色条件缓存、批量解码 `infer_batch`、
可调 CFM 步数。要和上游对版本时，clone 官方仓库到临时目录和它 diff 即可。

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

### 速度：为什么"加并发"没用，该动哪三个旋钮

实测（8G 4060 Laptop）一句话 7.9s 音频、单路 RTF **0.54**；把并发提到 3 路，同三句话的
总耗时与串行**完全一样**——GPT 解码每步都要读 1.5GB 权重（300 步 ≈ 1.8s），单路就把显存带宽
吃满了，再加并发只是互相争。所以提速要减少"每秒音频的计算量"：

| 旋钮 | 默认 | 说明 |
|---|---|---|
| `AIAB_TTS_NUM_BEAMS` | `1` | GPT 采样束宽。上游写死 3（解码 ×3），1 = 纯采样 |
| `AIAB_TTS_DIFFUSION_STEPS` | `16` | CFM 迭代步数。上游写死 25；16 比 25 快 ~29%，12 更快 |
| `AIAB_TTS_INFERENCE_CFG_RATE` | `0.7` | CFM 的 CFG 强度。降到 0 省一半 CFM 计算，但语气会变平 |
| `AIAB_TTS_VOICE_CACHE` | `8` | 参考音频条件按音色缓存的数量（多角色书必备） |

运行中对比不同档位：`GET /debug/tuning` 看当前值，`POST /debug/tuning {"diffusionSteps": 12}`
临时改（不用重载模型，传 `null` 恢复默认）。听感对比页：`data/ab_samples/speed_compare.html`。

另外上游把参考音频条件存成**单槽位**，多角色书每换一个音色就重算参考、还会
`torch.cuda.empty_cache()`；本项目已改为按音色缓存（见 `tts/README.md`），这是 1.23x → 1.78x 的主要来源。

### 批量解码：`POST /v1/synthesize_batch`

进一步的大头是**批量解码**：GPT 每步都要读一遍权重，一批 N 条只读一次。客户端（worker）
会把同一个音色的连续几句打包（默认 4 条一包）发过来，服务端一次解码后返回 zip
（`000.wav`… + `manifest.json`，时长也在 `X-Item-Durations` 响应头里）。

| 方式 | 吞吐（实时倍数） |
|---|---|
| 逐行 3 路并发 | 1.49x |
| 批量 4 条一包 | **3.82x** |
| 批量 4 条一包（40 字/句） | **4.61x** |

配置：`AIAB_TTS_MAX_BATCH_ITEMS`（默认 8，一次最多几条）、`AIAB_TTS_BATCH_MAX_SEGMENTS`
（默认 8，一段 GPT 解码里最多几段）。长句按上游规则切段后一起批量解码，**不再退回单条推理**
（实测长句占 64% 音频量，退回单条会把吞吐从 3.6x 打到 1.8x）。主项目侧是 `AB_SYNTH_BATCH_SIZE`（默认 4）
与 `AB_SYNTH_BATCH_WORKERS`（默认 1：一个包内部已经并行 N 条，再叠并发只会抢显存）。

### 内存自检：权重到底在显存还是又回主机内存了

`GET /debug/memory` 直接报答案：`model.cpuResidentMB` 是还留在主机内存里的参数量（正常应当是 `0`），
`model.modules` 逐个列出 w2v / codec / s2mel / campplus / bigvgan 的设备、精度与大小，
`host` 是进程的常住工作集 / 提交。每次加载模型时也会把这份自检写进日志（`内存自检：{...}`）。

注意区分三件事：显存占用（`model.cuda.reservedMB` 与 `nvidia-smi`）、**主机常驻**（`host.workingSetMB`）、
**提交量**（`host.commitMB`，Windows 上把显存分配也算进去，所以经常十几 GB，不代表真的占了这么多内存）。
主机常驻里含 torch 的 CUDA DLL 代码页（共享、可回收）与 CUDA 上下文，这部分不是权重副本。

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
