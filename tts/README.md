# aiab-tts（IndexTTS-2.5 推理服务）

**「AI 多人语音有声书」的合成引擎**：一句话一个请求，按角色给音色（零样本克隆）、按台词给情绪，
逐句合成后由主项目拼成一章 / 一整本书。

独立项目：**独立 venv、独立进程、独立部署**。主项目（仓库根目录的 `aiab`）只通过 HTTP 调用它，
任何情况下都不会 import 本项目的代码，也不在同一进程里加载模型。

## 多人语音靠的三件事

| 能力 | 接口 / 字段 | 说明 |
|---|---|---|
| **每个角色一个音色** | `POST /v1/refs` → `refId` | 上传一次参考音频（建议 5–15 秒干净人声）即成一个音色；同一 `refId` 在后续所有合成里复用，进程内按（文件 mtime、大小）缓存 |
| **逐句情绪** | `emoVector`（8 维） | `happy/angry/sad/afraid/disgusted/melancholic/surprised/calm`；只对人物台词有意义，旁白不传 |
| **语速 / 注音** | `rate`、`pronunciation` | `rate` 0.5–2.0（引擎侧换算 `duration_factor = 1/rate`）；注音支持 `pinyin / cmu / kana` 内联写法 |

另外两个实用字段：

- `seed`：合成前给 torch 播种，**同 seed + 同输入可复现**（做 fp32/bf16、换模型这类 A/B 对比时用）；
- `format`：目前只支持 `wav`（长文本请由客户端按 `maxTextChars` 分块，服务端直接拒绝超长，不会悄悄截断）。

## 快速开始（GPU 机器）

```powershell
cd tts
uv sync --python 3.11 --extra indextts --extra download  # 推理栈 + 下载客户端（index-tts 自己要求 <3.12）
git clone https://github.com/index-tts/index-tts.git index-tts
uv pip install --python .venv -e index-tts              # IndexTTS-2.5 不在 PyPI 上

# Windows 上 PyPI 的 torch 是 CPU 版，装 CUDA 版（否则 CPU 推理慢几十倍）
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 torch==2.8.* torchaudio==2.8.*

uv run --project tts aiab-tts download --source modelscope        # 或 huggingface / local
uv run --project tts aiab-tts serve --backend indextts --host 0.0.0.0 --port 8020
uv run --project tts aiab-tts check --url http://127.0.0.1:8020
```

服务本身不卡 Python 版本（`>=3.10`）；`<3.12` 是 `index-tts` 自己的声明，装不上时启动日志会带上 import 的真实报错。

完整步骤、模型来源三选一、manifest 校验与显存共享见 [`docs/tts-deploy.md`](../docs/tts-deploy.md)。

## HTTP 契约（后端唯一耦合面）

| 接口 | 说明 |
|---|---|
| `GET /health` | `status / modelLoaded / device / vramTotalMB / vramUsedMB / recommendedConcurrency / inflight / avgInferenceSecPerAudioSec / engine / engineVersion / modelSource / uptimeSec` |
| `GET /capabilities` | 情绪维度、语速范围、注音方式、语言、采样率、单次文本上限、`supportsSeed` |
| `POST /v1/refs` | multipart 上传参考音频一次 → `refId`（服务端缓存） |
| `POST /v1/synthesize` | `text / refId / lang / emoVector / rate / pronunciation / seed / format` → WAV 字节流 + `X-Duration-Sec` 等响应头 |
| `POST /warmup` / `POST /unload` | 预加载 / 释放模型与显存 |

约定：

- `recommendedConcurrency` 是后端自报的安全并发，**冷启动时也如实上报**；`status` 才是加载状态。
- 错误统一为 `{"detail": {"code": "...", "message": "..."}}`，
  `code ∈ bad_request | bad_ref | busy | oom | engine_error | not_loaded`。
- 引擎推理失败（`engine_error`）现在会把**完整堆栈**写进服务日志（以前只回一句 message，没法定位）。

## 并发、显存与加载（本服务最硬的工程点）

上游 IndexTTS-2.5 是"单实例、一次一条"的写法，这里做了三层改造，才能在 8G 卡上跑多人语音：

### 1. 3 路真并发（单进程、单份权重）

上游 GPT 推理把"当前请求的条件嵌入"存在实例属性 `cached_mel_emb` 上，两个请求并发会互相覆盖，
直接 500（`The size of tensor a (46) must match the size of tensor b (39)`）。
`aiab_tts.indextts_compat.make_gpt_inference_thread_safe()` 把它补成 `threading.local`，
每个推理线程各存各的，于是**同一份模型**可以真并发。

- 并发上限：`AIAB_TTS_MAX_CONCURRENCY`（默认 **3**；补丁打不上时自动退回 1）；
- 实测：3 条长旁白并发 54.4s（串行约 165s），`/health` 峰值 `inflight=3`；
- 8G 卡上 3 路长句并发会打满显存（峰值 ~7.0–8.1GB），真 OOM 时主项目的池会降档 + 熔断 60 秒。

### 2. w2v-bert bf16（默认开）

上游只把 GPT 降成 bf16，其余模块还是 fp32。`apply_bf16_modules()` 默认把
**w2v-bert 语义/说话人编码器**降到 bf16（权重 + 输入自动对齐 dtype）：

- 空闲显存 5411 → **4837 MiB**；3 条长旁白并发峰值 8144 → **7030 MiB**；
- 听感 A/B：同一句台词、同一音色、同一情绪、同一 seed 的两个样例，w2v-bert 输出嵌入余弦 **0.9986**，人耳验收通过；
- 想再省显存可以 `AIAB_TTS_BF16_MODULES=w2v,codec,s2mel,campplus`（`bigvgan` 声码器最敏感，默认不建议开）。

### 3. 加载优化（权重不再在主机内存里多住一份）

上游是"CPU 随机初始化 → `torch.load` 整份读进主机内存 → `load_state_dict` → `.to(cuda)`"。现在：

- 大 checkpoint 用 `torch.load(..., mmap=True)` 按页读；
- GPT / codec / s2mel / campplus 直接在 `cuda:0` 设备上下文里构造，w2v-bert 用 `device_map` 直进显存；
- 实测：加载期峰值主机内存 **7.04 → 3.94 GB**，加载后提交内存 **12.53 → 8.34 GB**，加载耗时 **24.1 → 18.7 s**；
- `AIAB_TTS_FAST_LOAD=0` 可以关掉这条快路（逃生开关）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIAB_TTS_BACKEND` | `indextts` | 只支持 `indextts`（IndexTTS-2.5） |
| `AIAB_TTS_HOST` / `AIAB_TTS_PORT` | `127.0.0.1` / `8020` | 监听地址 |
| `AIAB_TTS_DATA_DIR` | `data` | 参考音频缓存目录 |
| `AIAB_TTS_MODEL_SOURCE` | `local` | `modelscope` / `huggingface` / `local` |
| `AIAB_TTS_MODEL_DIR` | `checkpoints` | 权重目录（含 `manifest.json`） |
| `AIAB_TTS_MODEL_ID` | `IndexTeam/IndexTTS-2.5` | 下载用的仓库名 |
| `AIAB_TTS_HF_ENDPOINT` | 空 | HuggingFace 镜像地址 |
| `AIAB_TTS_MAX_CONCURRENCY` | `0` | 0 = 用后端默认（3 路真并发） |
| `AIAB_TTS_QUEUE_TIMEOUT_SECONDS` | `600` | 排队超时 → 返回 `busy` |
| `AIAB_TTS_USE_BF16` | `true` | GPT + w2v-bert 的推理精度 |
| `AIAB_TTS_BF16_MODULES` | `w2v` | 额外降 bf16 的模块：`w2v,codec,s2mel,campplus,bigvgan` |
| `AIAB_TTS_FAST_LOAD` | `1` | 加载优化（mmap + 直接进显存）开关 |
| `AIAB_TTS_VERIFY_MANIFEST` | `true` | 启动/下载时校验权重 |

## 命令

| 命令 | 作用 |
|---|---|
| `aiab-tts serve --backend indextts --host --port [--lazy] [--access-log]` | 起服务（`--lazy` 第一次合成才加载模型） |
| `aiab-tts check --url ...` | 打印健康状态与能力集 |
| `aiab-tts download --source ... [--model-dir] [--hf-endpoint]` | 下载/校验权重（幂等、断点续传） |
| `aiab-tts unload --url ...` | 释放显存（与别的 GPU 任务共享机器时用；注意 unload 只还显存，主机内存要重启进程才还） |

## 测试

```powershell
uv run --project tts pytest tts/tests
```

60 个用例覆盖：HTTP 契约、并发门与显存保护、IndexTTS 参数映射（`rate → duration_factor`、注音内联写法、
seed 播种）、并发安全补丁（线程本地条件嵌入）、bf16 模块降精度、加载优化（mmap / 设备构造）、
模型来源与 manifest 校验。真实 GPU 推理由 `docs/tts-deploy.md` 的验收清单覆盖。
