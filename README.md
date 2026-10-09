# AI 有声书（ai-audiobook）

**把一本小说变成"AI 多人语音"有声书**：不是一个人从头念到尾，而是逐句识别说话人，
让每个角色用自己的音色、按这一句该有的语气把台词说出来，旁白保持讲述感。

导入 txt / epub → 自动分章 → 大模型逐句分析（谁说的 + 这一句怎么说）→ 角色整合 →
每个角色写一段音色描述（可试听、可改、可重写）→ 逐句按「角色音色 + 本句语气」生成 →
每章渲染 → 导出可播放的 `wav / srt / mkv`。

## 核心能力：AI 多人语音

一本书里可能有几十个角色。这个项目的重点就是把「谁在说、用什么音色、这一句怎么说」逐句做对：

| 能力 | 怎么做的 |
|---|---|
| **逐句说话人** | 大模型直出每句话的说话人（`旁白` / 角色名），不做规则分词；`小鹿：走。` 这类前缀会被强制剥掉，只保留台词本身 |
| **每个角色独立音色** | 不给参考音频也能配音：先让大模型出一张**选角表**（给角色定音色原型，强制两两拉开音区/质地，避免同类角色撞成一个声音；只协调有戏份的角色并分批做，600+ 角色的书也不会一次塞爆提示词），再按原型给每个角色写**基础音色描述**（性别年龄感、音区、音色质地、咬字），Qwen3-TTS VoiceDesign 逐句按描述生成；描述可自己改、可让模型"微调"或"按你写的要求换一版"，改完只重跑这个角色 |
| **逐句语气** | 提取阶段同一趟里让大模型给每句话写**表演描述**（语气/语速/音量/气息，15 字内）；合成时和角色基础描述拼成一句送给模型，所以"这句怎么说"是逐句决定的 |
| **同人异名合并** | 「本名 / 绰号 / 尊称」交给大模型归并成一个角色，避免同一个人被拆成好几个音色 |
| **音色库管理** | 试听、上传、停用（回收站）；手工给某个角色绑库存音色时，这个角色就走参考音频克隆（备用通道） |

实际效果就是：旁白有旁白的音色描述，`小鹿` 有她自己的；同一句话里"压低声音、尾音发颤"这类语气
由逐句分析决定，不会整本书一个腔调。改一句只重合成那一句，改某个角色的音色描述只重跑他一个人。

## 流水线

```
导入 txt/epub ─► 分章(本地，自动) ─► 逐句提取(说话人+本句表演，LLM) ─► 角色整合(同人异名，LLM) ─► 音色描述(LLM) ─► 逐句按描述生成 ─► 每章渲染 ─► 整本合本
                                 └────────────────────「分析台词」────────────────────┘                    └────────「生成音频」────────┘
                                                                                                          每个角色 → 基础音色描述 + 本句语气
```

- 导入后**只自动清洗 + 分章**（纯本地文本处理，不烧 LLM），分析和合成都必须用户点按钮；
- 导入时先做一遍**正文清洗**：HTML 标签与实体、站点广告、章节导航（上一章/返回目录）、
  「求月票/求订阅」、网址水印、整段重复都会被剔除，清洗统计写在 `chapters.json` 里可复核；
- 台词切分、说话人归属、每句的表演描述**全由大模型直出**（没有规则分词/归属）；
- 音色不再来自"参考音频 + 情绪向量"：每个角色由大模型写一段**基础音色描述**（存在 `casting.json`），
  合成时和本句的表演描述拼起来交给 Qwen3-TTS 的 VoiceDesign 逐句生成；
- 「分析本章台词」只重跑当前章，跑完会**当场为本轮新出现的角色补音色描述**（角色栏可试听、可改、可重写）；
  「分析全本台词」弹窗可只勾几章——勾多章时会合成**一个批量任务**，job 内部按 `AB_LLM_CONCURRENCY`
  并发同时提取（和整本同一模式），章节列表里能看到「正在分析台词」的沙漏，边出边显示；
- 重跑会覆盖勾选章节的逐句标注（含人工修改），弹窗里有明确提示；
- 改一句台词 / 改一个角色的音色描述 → 只作废受影响的章节成品，其余音频不动
  （缓存键里带"角色描述指纹 + 本句表演描述"，只有变了的行会重跑）。
- 老书（换 Qwen3-TTS 之前分析过的）里的提取结果没有逐句表演描述：下次分析时会**自动重新提取**那些章
  （认的是"带 emotion、没有 voice"，符合新格式的章一个请求都不多发）；

## 架构

| 进程 | 启动 | 端口 | 职责 |
|---|---|---|---|
| 后端 | `uv run aiab serve --port 8300` | 8300 | FastAPI + 静态 UI；写入只落盘和入队，不跑长任务 |
| worker | `uv run aiab worker` | — | 领 SQLite 队列：分章、台词提取、角色整合、音色描述、合成、渲染、合本 |
| TTS | `uv run aiab tts start` | 8020 | Qwen3-TTS 推理（VoiceDesign 逐句按描述生成）；独立 venv、独立进程，与后端不共进程 |

任务队列用 **SQLite（WAL + 租约）**，没有 Celery / Redis / 消息中间件；
进度判断只看文件（JSON / JSONL / WAV / SRT），所以中断后重跑天然断点续传。

## 性能（8G 显存笔记本实测）

多人语音的瓶颈是 TTS。Qwen3-TTS 是 1.7B 的自回归模型，**批量解码 + code_predictor
CUDA Graph** 是两个有效的加速杠杆（解码每一步都要把权重读一遍，一批 N 条只读一次）：

| 配置 | 吞吐 | 显存峰值 |
|---|---|---|
| 上游实现，一包 4 条 | 2.60x 实时 | 5.6 GB |
| **code_predictor 图路径，一包 4 条** | **4.37x 实时** | 5.3 GB |
| **code_predictor 图路径，一包 8 条** | **7.36x 实时**（4 轮 6.84~7.86x） | 7.7 GB |

- 客户端把**同一个角色、长度相近**的连续几句打包成一次请求（`AB_SYNTH_BATCH_SIZE=8`），
  服务端一次 `generate_voice_design(text=[...], instruct=[...])` 出 N 条；
- 一包里的每条可以有**各自的音色描述**（角色基础描述 + 本句语气），互不影响；
- 25 分钟的音频，**一包 8 条约 3.4 分钟**出完（图路径实测）；同等配置在上游实现下要 9.6 分钟；
- 为什么是「图」而不是「并发」：分析器显示每帧要发 1.7 万个微内核、GPU 占空只有 30~45%，
  加并发只会让更多微内核挤同一条提交路径（并发 1/2/3/4 → 2.67/2.68/2.21/1.93）。
  `AIAB_TTS_FAST_PREDICTOR=graph` 把每个 codebook 步的上百个内核折成一次图回放，
  与上游**逐样本比特一致**（贪婪对拍 `max|Δ| = 0`）。详见 [tts/README.md](tts/README.md)；
- 长旁白（>150 帧 ≈ 12 秒）会让整包解码溢出到共享显存，整包慢 2~3 倍；
  `AIAB_TTS_DECODE_CHUNK=2` 让 codec 解码每次只解 2 条，峰值从 12.2GB 降到 7.3GB，
  第 3 章最长的 6 包 **165s → 88s**（3.82x → 7.16x 实时），短包不受影响；
- 显存：VoiceDesign 权重常驻约 4.0 GB，8 条一包峰值约 7.7 GB，靠服务端
  `release_after_request` 请求边界归还回收；Base（克隆通道）只有真的
  手工绑了库存音色时才加载，默认和 VoiceDesign 互斥换入（8G 卡放不下两个 1.7B）。

批量合成的开关在 `.env`：`AB_SYNTH_BATCH_SIZE=8`（每包几条，1 = 关）、`AB_SYNTH_BATCH_WORKERS=1`
（同时几个包）。超过单条上限（`maxTextChars`）的超长行按客户端分块拼接，长句不再退回单条。
批量失败会自动退回逐行，结果不受影响。

## 目录结构

```
src/audiobook/        后端 + worker + CLI（FastAPI、队列、LLM 分析、TTS 客户端、渲染）
src/audiobook/web/    浏览器 UI（原生 ES 模块 + CSS 变量，无构建、无前端框架）
tts/                  独立 TTS 服务（Qwen3-TTS 主线 / IndexTTS-2.5 可选，自带 pyproject 与 venv）
tests/                主项目测试；tts/tests/ 是 TTS 子项目测试
docs/                 界面、TTS 部署、导出、迁移文档
tools/                UI 探针、角色对比等实用脚本
data/                 运行时数据（不入 git）
```

## 快速开始

前置：Python 3.13 + [uv]；TTS 需要一块 NVIDIA GPU（8G 显存可跑，VoiceDesign 常驻约 4 GB）。

```powershell
# 1) 主项目
uv sync

# 2) TTS 子项目：Qwen3-TTS（详见 docs/tts-deploy.md）
cd tts
uv sync                                   # 只装服务框架（不拉 torch）
powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1
cd ..
```

安装脚本会做三件事：建 `tts/.venv-qwen`（Python 3.12）、装 `qwen-tts` + CUDA 版 torch
（Windows 上 PyPI 的 torch 是 CPU 版，必须换 CUDA 轮子）、把两个权重下到
`tts/checkpoints/Qwen3-TTS-12Hz-1.7B-{VoiceDesign,Base}`（共约 7.7 GB）。

配置 LLM（任意 OpenAI 兼容端点）：复制 `.env.example` 为 `.env`，改 `AB_LLM_BASE_URL` / `AB_LLM_MODEL` / `AB_LLM_API_KEY`。

```powershell
uv run aiab serve --port 8300      # 窗口 1
uv run aiab worker                 # 窗口 2
uv run aiab tts start --wait 600   # 窗口 3；首次会先下载权重再加载模型
```

然后打开 <http://127.0.0.1:8300>。（也可以用 `serve` + `worker` 起好，在设置页点「一键启动 TTS 服务」，
启动参数会写入 `data/settings.json`，worker 下一轮任务自动生效，不用重启。）

## 日常使用

1. **书架**导入 txt / epub → 自动分章（卡片本身可点进书页，卡上只有「打开 / 删除」）；
2. 进工作台点「**分析本章台词**」（只重跑当前章）或「**分析全本台词**」（弹窗里可只勾几章返工）。
   分析链跑完：逐句说话人 + 本句表演描述 → 角色表 → 每个角色的基础音色描述；
3. 右侧「本章角色 / 全书角色」里逐个角色看音色描述：点「**试听**」按当前描述生成一段试听，
   不满意就改文字再试听，或点「**让模型写一版**」重写；描述一改，这个角色的旧音频会在下次生成时按新描述重跑；
   按需改台词、逐句试听 / 重生成也可以在工作台里做；
4. 点「**生成本章音频**」或「**生成整本音频**」开始合成：逐句按角色切换音色 → 拼接 → 每章 `wav + srt`，最后合出整本；
5. 「**更多 ▾ → 导出整本成品**」→ `data/books/<bookId>/output/`：`chapter_XXXX.wav/.srt/.mkv`、`book.wav/.srt/.mkv`、`playlist.m3u`；
   跑完之后「**更多 ▾ → 打开成果文件夹**」可以直接在资源管理器里打开这个目录；
6. 播放：`mkv` 内嵌软字幕，手机用 VLC / nPlayer / MX Player 直接打开就能看字幕；`wav + srt` 同名放一起也可以；
7. **删除整本**：书架卡片上的「删除」（二次确认）。它会删掉这本书的数据目录、`books` 行与所有 `jobs` 行；
   如果这本书还有任务在跑，会返回提示让你先去任务中心取消，避免 worker 在目录被删后继续写入。

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run aiab serve --port 8300 [--host 0.0.0.0]` | 起后端 + 浏览器 UI（手机连局域网 IP 即可） |
| `uv run aiab worker [--once] [--max-jobs N]` | 起 worker；`--once` 只领一个任务，便于验收 |
| `uv run aiab import <book.txt\|book.epub> --title 书名` | 命令行导入（txt / epub）并入队分章 |
| `uv run aiab run <bookId>` | 按文件断点补跑下一步（分析 + 合成） |
| `uv run aiab export <bookId> [--mode chapter/book/all] [--container mp4]` | 导出成品 |
| `uv run aiab tts start/stop/status/logs` | 一键启停本机 TTS 服务 |
| `uv run aiab llm-check` | 验证 LLM 端点连通并做一次 JSON 往返 |
| `uv run aiab migrate voices/book` | 从旧系统迁移音色 / 书籍 |
| `uv run aiab snapshot export/import` | 单本书的 JSON 快照导出导入 |

## 数据在哪

```
data/
├── service.db                        SQLite：books / jobs
├── settings.json                     设置页写入的覆盖项（含 TTS 端点）
├── books/<bookId>/
│   ├── source/original.txt           书稿纯文本（epub 导入时是抽出来的正文）
│   ├── chapters.json                 分章结果
│   ├── analysis/characters.json      角色表（主名 + 别名 + 出现章数）
│   ├── analysis/extract/chapter_XXXX.json  整章提取原始结果（每句 + 说话人 + 本句表演，LLM 直出）
│   ├── analysis/lines/chapter_XXXX.jsonl   逐句标注（说话人/本句表演/语速；名字已映射成 role_id）
│   ├── voices/casting.json           角色 → 基础音色描述（+ 手工绑库存音色时的 voice_source=library）
│   ├── voices/<roleId>/preview.wav   角色试听（按当前描述生成，点「试听」才有）
│   ├── audio/chapter_XXXX/<lineId>.wav     逐句音频（+ .meta.json 缓存键）
│   ├── output/                       成品 wav/srt/mkv、render.json、合本、播放列表
│   └── logs/llm.jsonl                LLM 调用日志
└── voices/<voiceId>/{voice.json,ref.wav}   音色库（内置 + 页面上传；停用的是 disabled=true）
```

## 设计约束（有意为之）

1. **长任务只在 worker**：接口永远快返回，分析/合成/渲染都入队，关掉浏览器也照跑。
2. **不要外部工作引擎**：SQLite WAL + 租约足够本地单用户用，少一层运维。
3. **文件即事实来源**：进度看文件是否存在，任务表只是调度；重跑幂等。
4. **只有真引擎**：产品里没有 fake 后端，合成必须走独立 TTS 服务；测试替身只存在于 `tests/`。
5. **音色走描述、不走参考音频**：每条台词的音色 = 角色基础描述 + 本句表演描述，交给 Qwen3-TTS
   VoiceDesign 逐句生成。这样每句话都有自己该有的语气，不会被一段参考音频的固定语气"传染"；
   代价是同一角色的音色会有轻微浮动，这是明确接受的取舍。参考音频克隆作为备用通道保留
   （手工给角色绑库存音色时走它）。两段描述职责不重叠：基础描述只写"声音是什么样"
   （性别年龄感/音区/质地/咬字），语速与情绪只由本句表演描述负责，所以不会互相打架。
6. **界面不暴露底层旋钮**：响度、停顿、ffmpeg 路径都有按有声书场景调好的默认值，要改才动 `.env`。

## 测试

```powershell
$env:PYTHONIOENCODING="utf-8"
uv run pytest                                              # 主项目
uv run --project tts pytest tts/tests                      # TTS 子项目
$env:AB_UI_SMOKE="1"; uv run pytest tests/test_ui_smoke.py  # 浏览器冒烟（需 Chrome + Node ≥ 22）
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ui.md](docs/ui.md) | 界面结构、快捷键、设计系统、后端接口清单 |
| [docs/tts-deploy.md](docs/tts-deploy.md) | TTS 环境准备、模型三选一、并发/bf16/加载优化与验收清单 |
| [docs/export.md](docs/export.md) | 停顿 / 响度 / 容器导出与播放说明 |
| [docs/migrate.md](docs/migrate.md) | 旧系统音色与书籍迁移 |

## 约定

- 提交信息末尾带 `Co-authored-by: Codex <codex@openai.com>`。
- 国内网络慢时可用阿里云 PyPI 镜像：`$env:UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple"`（或 `uv add --default-index ...`）。

[uv]: https://docs.astral.sh/uv/
