# aiab-tts（Qwen3-TTS 推理服务）

**「AI 多人语音有声书」的合成引擎**：逐句给一段"音色描述"，按描述生成这一句的音频。

独立项目：**独立 venv、独立进程、独立部署**。主项目（仓库根目录的 `aiab`）只通过 HTTP
调用它，任何情况下都不会 import 本项目的代码，也不在同一进程里加载模型。

## 为什么是"按描述生成"而不是"克隆参考音频"

用参考音频克隆时，参考音频里那股固定的语气会顺着条件通道渗进每一句 —— 音色是对了，
但整本书一个腔调（IndexTTS 时代实测：分析出的情绪权重总和中位数只有 0.6，剩下 40% 的语气
来自参考音频）。Qwen3-TTS 的 VoiceDesign 换了个思路：

```
角色基础音色描述（性别年龄感/音区/音色质地/咬字，大模型按角色写，一本书写一次；
                写之前先出一张全书"选角表"，保证同类角色之间不会撞成一个声音）
        +
这一句的表演描述（语气/语速/音量/气息，逐句分析时大模型直出）
        ↓
generate_voice_design(text=台词, instruct=拼好的描述)  →  这一句的音频
```

- 音色由角色描述**锁定**，语气由本句描述**逐句决定**；
- 两段描述职责不重叠：语速/音量/情绪只写在本句描述里，基础描述不许写（也就不存在打架）；
- 代价是同一角色的音色会有轻微浮动，这是明确接受的取舍；
- 参考音频克隆（Base 模型）作为备用通道保留：手工给角色绑库存音色时走它。

## 快速开始（GPU 机器）

```powershell
cd tts
uv sync                                                    # 只装服务框架（不拉 torch）
powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1
uv run --project tts aiab-tts serve --backend qwen3 --host 0.0.0.0 --port 8020
uv run --project tts aiab-tts check --url http://127.0.0.1:8020
```

安装脚本做三件事：

1. 建 `tts/.venv-qwen`（Python 3.12，和 IndexTTS 的 `.venv` 分开：两套依赖互相打架）；
2. 装 `qwen-tts==0.1.1` + **CUDA 版** torch 2.8（Windows 上 PyPI 的 torch 是 CPU 版，
   跑起来会报 `Torch not compiled with CUDA enabled`，所以用 `--find-links` 指到 cu128 轮子）；
3. 下两个权重到 `tts/checkpoints/`：
   `Qwen3-TTS-12Hz-1.7B-VoiceDesign`（按描述生成，主力）+ `Qwen3-TTS-12Hz-1.7B-Base`
   （参考音频克隆，备用），共约 7.7 GB。

## HTTP 契约（主项目唯一耦合面）

| 接口 | 说明 |
|---|---|
| `GET /health` | `status / modelLoaded / device / vramTotalMB / vramUsedMB / recommendedConcurrency / inflight / avgInferenceSecPerAudioSec / engine / engineVersion / uptimeSec` |
| `GET /capabilities` | `voicePrompt`（支持逐句按描述生成）、`voiceDesign`（支持试听设计）、`batch`、`maxBatchItems`、`sampleRate`、`maxTextChars` |
| `POST /v1/synthesize` | `text / voicePrompt / lang / format`（纯描述）或 `text / refId / lang`（克隆）→ WAV + `X-Duration-Sec` 等响应头 |
| `POST /v1/synthesize_batch` | 一个包多条：`{refId?, lang, items:[{text, voicePrompt?}]}` → zip（`000.wav`… + `manifest.json`） |
| `POST /v1/design` | 角色试听：`{text, instruct, lang}` → 一段 WAV（同一套 VoiceDesign 逻辑） |
| `POST /v1/refs` | multipart 上传参考音频（克隆通道用）→ `refId`（带 `refText` 时走文本对齐克隆，质量更高） |
| `POST /warmup` / `POST /unload` | 预加载 / 释放模型与显存 |

约定：

- `recommendedConcurrency` 是后端自报的安全并发，**冷启动时也如实上报**；`status` 才是加载状态；
- `refId` 与 `voicePrompt` 至少给一个：给 `refId` 走克隆，只给 `voicePrompt` 走按描述生成；
- 错误统一为 `{"detail": {"code": "...", "message": "..."}}`，
  `code ∈ bad_request | bad_ref | busy | oom | engine_error | not_loaded`；
- 引擎推理失败（`engine_error`）会把**完整堆栈**写进服务日志，而不是只回一句 message。

## 显存与速度（8G 卡实测）

模型：VoiceDesign 1.7B，bf16，权重常驻约 **4.0 GB**；Base 只有真的用到克隆才加载，
默认和 VoiceDesign **互斥换入**（两个 1.7B 同时常驻会挤爆 8G）。

自回归解码每步都要把权重读一遍，所以**批量是有效的加速杠杆**（一包 N 条只读一遍权重）。

### 每帧 1.7 万个微内核：先修这个，再谈并发

torch.profiler 实测一包 4 条（34 秒音频 / 128 帧，RTX 4060 Laptop 8G）：

```
每帧内核启动次数        17,007 次     平均每个内核只有 6.1 μs
CPU 自身耗时 / 墙钟     16.5s / 21.8s（76%）
  cudaLaunchKernel      981,273 次 3.07s
  cudaStreamSynchronize 4,666 次   0.98s（平均 210 μs/次，每次都抽干流水线）
GPU 占空               30~61%
```

根因不是并发不够，而是 Talker 每生成 1 帧（80 ms 音频）内部要调 **15 次
`code_predictor.generate()`**——每次都是完整的 HF `generate()`（deepcopy 配置、
建 warper、建 cache、判停同步、拼返回对象），加上 28 层 Talker 本身，一帧就是
1.7 万次微内核。**并发只会让更多微内核挤同一条提交路径**：

| 并发路数（上游路径，pack 4） | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| 吞吐（音频秒/墙钟秒） | **2.67** | 2.68 | 2.21 | 1.93 |
| GPU 利用率均值 | 45% | 43% | 39% | 32% |

### 解法：code_predictor 手工 CUDA Graph（`AIAB_TTS_FAST_PREDICTOR=graph`）

把每个 codebook 步的 `projection + 5 层 trunk` 前向捕获成一张 CUDA Graph
（定长 17 位置前缀、无 KV cache、采样留在图外），之后每步只 `replay()` 一次。
**与上游逐样本比特一致**（贪婪解码下 `max|Δ| = 0.00e+00`，4/4 条长度完全相同）。

> 走过的弯路：`torch.compile` 在本机走不通——Windows 没有 triton；补装
> `triton-windows==3.4.0.post21`（阿里云镜像）后 `reduce-overhead` 仍然
> `OverflowError: Python int too large to convert to C long`（32 位 long），
> 而 `default` 模式编译出来的 token 与上游不一致（因果掩码被改写，不可接受）。
> 所以最终用原生 `torch.cuda.CUDAGraph` 手工捕获。

### 实测（8G 卡，HTTP 服务全链路，含打包/解包）

| 配置 | 吞吐 | 说明 |
|---|---|---|
| 上游 + pack 4 | 2.60x 实时 | 改动前的线上配置 |
| 图路径 + pack 4 | **4.37x 实时** | 1.68x |
| 图路径 + pack 8 | **7.36x 实时** | 4 轮 6.84~7.86x，显存 5.3↔7.7GB |
| 图路径 + pack 4 + 并发 2 | 4.55 音频秒/墙钟秒 | 并发在图路径下才有正收益 |

- 一包里每条可以有各自的 `voicePrompt`（`generate_voice_design` 原生支持列表入参）；
- 客户端把"同一个角色 + 长度相近"的连续几句打包成一次请求（默认 8 条）；
- 8 条一包时每一步权重只读一遍却摊到 8 条文本上，所以吞吐接近翻倍；显存靠
  `release_after_request` 请求边界归还回收（累计归还十几 GB，不再溢出共享显存）。

### 第二种慢：长包溢出共享显存（`AIAB_TTS_DECODE_CHUNK`）

跑完第 3 章（400 行 / 42.9 分钟音频）后发现：前 40 包每包 ~8s，**最后 10 包退化到
21~56s**。逐包复盘发现打包是按字符数排序切的，最后几包天然是书里最长的旁白
（最长一行 94 字 / 20.8 秒音频 / 260 帧）。但按帧归一化后每帧成本仍从 75ms 涨到 181ms，
说明不只是"包更长"：

| pack（8 条 74~94 字旁白） | ms/帧 | 物理显存峰值 | 分配器 allocated | reserved |
|---|---|---|---|---|
| 2 | 55.9 | 6042MB | 4832MB | 5878MB |
| 4 | 62.1 | 7632MB | 5537MB | 7464MB |
| 8 | **169.8** | 7946MB | 7573MB | **12212MB ← 溢出到共享显存** |

根因不是 Talker：逐帧采样显示自回归阶段每步只涨 ~1.8MB，整包峰值 7.0GB 出现在
`speech_tokenizer.decode` —— 8 条 × 228 帧 × 1920 倍上采样，末级张量单个就近 450MB。
超了物理显存（8.2GB）之后驱动自动走共享显存（PCIe 内存），于是整包慢 2~3 倍。

修法：**解码按 2 条一组切块**（解码结果逐条独立，拼接完全等价；实测长度逐样本一致），
并且只在"最长的一条 ≥150 帧"时才切 —— 短包整包解码更快，两种场景都拿最好成绩。

| 场景 | 修前 | 修后 |
|---|---|---|
| 第 3 章最长的 6 包（8 条/包） | 165s / 3.82x | **88~101s / 6.24~7.16x** |
| 短包连跑 16 包 | 7.11x | 6.86x（持平） |

顺带试过但**没启用**的两条路：`expandable_segments:True` 在 Windows 上不生效
（启动就 warning）；把 Talker 的 KV cache 换成预分配 + `index_copy_`（干掉
`DynamicCache` 每步 `torch.cat` 的 O(n²) 拷贝）——贪婪对拍逐样本比特一致，但只快 3%，
说明长包的瓶颈是解码显存而不是 cache 拷贝，留在 `qwen3_cache.py` 里备用。

其它相关设置：`AIAB_TTS_MAX_CONCURRENCY`（默认 3）、`AIAB_TTS_MAX_BATCH_ITEMS`（默认 8）、
`AIAB_TTS_MAX_TEXT_CHARS`（默认 300，超长由客户端分块）、`AIAB_TTS_FAST_PREDICTOR`
（`graph` / `loop` / `pad` / `off`）、`AIAB_TTS_DECODE_CHUNK`（默认 2，0 = 不分块）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIAB_TTS_BACKEND` | `qwen3` | `qwen3`（Qwen3-TTS）/ `indextts`（IndexTTS-2.5） |
| `AIAB_TTS_HOST` / `AIAB_TTS_PORT` | `127.0.0.1` / `8020` | 监听地址 |
| `AIAB_TTS_DATA_DIR` | `data` | 参考音频缓存目录 |
| `AIAB_TTS_MODEL_SOURCE` | `local` | `modelscope` / `huggingface` / `local` |
| `AIAB_TTS_MODEL_DIR` | `checkpoints` | 权重根目录（qwen3 用它的两个子目录） |
| `AIAB_TTS_QWEN_BASE_ID` | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | 克隆模型（下载用） |
| `AIAB_TTS_QWEN_DESIGN_ID` | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 设计模型（下载用） |
| `AIAB_TTS_QWEN_ATTN` | 空 | 注意力实现（Windows 装不了 flash-attn，留空用默认） |
| `AIAB_TTS_QWEN_KEEP_BOTH` | `false` | 是否同时常驻两个模型（8G 卡别开） |
| `AIAB_TTS_MAX_CONCURRENCY` | `0` | 0 = 用后端默认（3） |
| `AIAB_TTS_MAX_BATCH_ITEMS` | `8` | 一个批量包最多几条（8G 卡建议 4） |
| `AIAB_TTS_QUEUE_TIMEOUT_SECONDS` | `600` | 排队超时 → 返回 `busy` |
| `AIAB_TTS_USE_BF16` | `true` | 推理精度（bf16 权重 3.9GB，fp32 会翻倍） |

## 命令

| 命令 | 作用 |
|---|---|
| `aiab-tts serve --backend qwen3 --host --port [--lazy] [--access-log]` | 起服务（`--lazy` 第一次合成才加载模型） |
| `aiab-tts check --url ...` | 打印健康状态与能力集 |
| `aiab-tts download --source ... [--model-dir]` | 下载/校验权重（IndexTTS 通道，走 manifest） |
| `aiab-tts unload --url ...` | 释放显存（与其他 GPU 任务共享机器时用） |

## 另一个后端：IndexTTS-2.5（保留，可选）

老的 IndexTTS-2.5 通道还在（`--backend indextts`），它走"参考音频克隆 + 8 维情绪向量"，
依赖装在另一个 venv（Python 3.11 / transformers 4.52）：

```powershell
uv sync --python 3.11 --extra indextts --extra download
uv pip install --python .venv -e index-tts
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 torch==2.8.* torchaudio==2.8.*
```

它当年做的优化（3 路真并发补丁、参考条件按音色缓存、批量解码 `infer_batch`、bf16 模块、
加载优化）连同实测数据都保留在 `docs/tts-deploy.md` 与 git 历史里；`tts/index-tts/` 是本仓库
自带的上游副本，我们的改动都带 `[AIAB 改动]` 注释。

## 测试

```powershell
uv run --project tts pytest tts/tests
```

用例覆盖：HTTP 契约、并发门与显存保护、qwen3 的两条通道（按描述生成 / 参考音频克隆）、
批量入参映射、语言代码映射、IndexTTS 参数映射与补丁。真实 GPU 推理由
[`docs/tts-deploy.md`](../docs/tts-deploy.md) 的验收清单覆盖。
