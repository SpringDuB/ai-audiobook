# AI 有声书（ai-audiobook）

**把一本小说变成"AI 多人语音"有声书**：不是一个人从头念到尾，而是逐句识别说话人，
让每个角色用自己的音色、自己的情绪把台词说出来，旁白保持中性叙述。

导入 txt / epub → 自动分章 → 大模型逐句分析（谁说的 + 什么情绪）→ 角色整合 → 音色推荐 →
逐句切换音色合成 → 每章渲染 → 导出可播放的 `wav / srt / mkv`。

## 核心能力：AI 多人语音

一本书里可能有几十个角色。这个项目的重点就是把「谁在说、用什么音色、什么情绪」逐句做对：

| 能力 | 怎么做的 |
|---|---|
| **逐句说话人** | 大模型直出每句话的说话人（`旁白` / 角色名），不做规则分词；`小鹿：走。` 这类前缀会被强制剥掉，只保留台词本身 |
| **每个角色独立音色** | IndexTTS-2.5 零样本克隆：上传一段参考音频（建议 5–15 秒干净人声，硬限制 1–60 秒）即成一个音色；角色行上直接给大模型推荐的 1–3 个音色（带理由），点一下换，不选就用第一条 |
| **逐句情绪** | 人物台词带 8 维情绪向量（`喜悦/愤怒/悲伤/恐惧/厌恶/忧郁/惊讶/平静` + 强度），旁白不带；合成时逐句送进引擎 |
| **同人异名合并** | 「本名 / 绰号 / 尊称」交给大模型归并成一个角色，避免同一个人被拆成好几个音色 |
| **音色库管理** | 试听、上传、停用（回收站）；停用的音色不会再被大模型看到，也不会被选中 |

实际效果就是：旁白用旁白音色，`小鹿` 一直用 `v032`，`苏锐` 一直用 `v041`，
碰到情绪句（`[0.5, 0, …]` 喜悦 0.5）音色也会跟着变语气；改一句只重合成那一句，换音色只重跑受影响的章。

## 流水线

```
导入 txt/epub ─► 分章(本地，自动) ─► 逐句提取(说话人+情绪，LLM) ─► 角色整合(同人异名，LLM) ─► 音色推荐(LLM) ─► 逐句合成 ─► 每章渲染 ─► 整本合本
                                 └────────────────「分析台词」────────────────┘                    └────────「生成音频」────────┘
                                                                                                    每个角色 → 自己的音色
```

- 导入后**只自动分章**（纯本地文本处理，不烧 LLM），分析和合成都必须用户点按钮；
- 台词切分、说话人归属、情绪标注**全由大模型直出**（没有规则分词/归属）；旁白不带情绪向量，只有人物话术带；
- 「分析本章台词」只重跑当前章；「分析全本台词」弹窗可只勾几章——勾多章时会合成**一个批量任务**，
  job 内部按 `AB_LLM_CONCURRENCY` 并发同时提取（和整本同一模式），边出边显示；
- 重跑会覆盖勾选章节的逐句标注（含人工修改），弹窗里有明确提示；
- 改一句台词 / 换一个音色 → 只作废受影响的章节成品，其余音频不动。

## 架构

| 进程 | 启动 | 端口 | 职责 |
|---|---|---|---|
| 后端 | `uv run aiab serve --port 8300` | 8300 | FastAPI + 静态 UI；写入只落盘和入队，不跑长任务 |
| worker | `uv run aiab worker` | — | 领 SQLite 队列：分章、台词提取、角色整合、音色推荐、合成、渲染、合本 |
| TTS | `uv run aiab tts start` | 8020 | IndexTTS-2.5 推理；独立 venv、独立进程，与后端不共进程 |

任务队列用 **SQLite（WAL + 租约）**，没有 Celery / Redis / 消息中间件；
进度判断只看文件（JSON / JSONL / WAV / SRT），所以中断后重跑天然断点续传。

## 性能（8G 显存笔记本实测）

多人语音的瓶颈是 TTS：一句一调、角色多、句子长。这里做了三层优化：

| 优化 | 效果（RTX 4060 Laptop 8G） |
|---|---|
| **3 路真并发**（单进程、单份权重） | 上游 GPT 推理把"当前请求的条件嵌入"存在实例属性上，并发会 500；已打线程本地补丁（`tts/src/aiab_tts/indextts_compat.py`），3 条长旁白并发 54.4s（串行约 165s） |
| **w2v-bert bf16**（默认开） | 空闲显存 5411 → 4837 MiB；3 条长旁白并发峰值 8144 → 7030 MiB；音质 A/B 通过（嵌入余弦 0.9986） |
| **加载优化** | checkpoint 用 mmap 读 + 模型直接在显存构造：加载峰值主机内存 7.04 → 3.94 GB，加载耗时 24.1 → 18.7 s |

想再省显存可以开更多 bf16 模块（`AIAB_TTS_BF16_MODULES=w2v,codec,s2mel,campplus`，声码器风险最高），
详见 [docs/tts-deploy.md](docs/tts-deploy.md)。

## 目录结构

```
src/audiobook/        后端 + worker + CLI（FastAPI、队列、LLM 分析、TTS 客户端、渲染）
src/audiobook/web/    浏览器 UI（原生 ES 模块 + CSS 变量，无构建、无前端框架）
tts/                  独立 TTS 服务（IndexTTS-2.5，自带 pyproject 与 venv）
tests/                主项目测试；tts/tests/ 是 TTS 子项目测试
docs/                 界面、TTS 部署、导出、迁移文档
tools/                UI 探针、角色对比等实用脚本
data/                 运行时数据（不入 git）
```

## 快速开始

前置：Python 3.13 + [uv]；TTS 需要一块 NVIDIA GPU（8G 显存可跑 3 路并发）与 Python 3.11（index-tts 自己的要求）。

```powershell
# 1) 主项目
uv sync

# 2) TTS 子项目（详见 docs/tts-deploy.md）
cd tts
uv sync --python 3.11 --extra indextts --extra download
git clone https://github.com/index-tts/index-tts.git index-tts
uv pip install --python .venv -e index-tts
cd ..
```

> Windows 上 PyPI 装到的是 CPU 版 torch，必须换 CUDA 版（`uv pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.* torchaudio==2.8.*`），
> 否则推理会退到 CPU。之后不要再跑 `uv sync`，细节见 [docs/tts-deploy.md](docs/tts-deploy.md)。

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
   分析链跑完：逐句说话人 + 情绪 → 角色表 → 每个角色的推荐音色（默认用第一条推荐）；
3. 进工作台按需改台词、换音色、逐句试听 / 重生成；右侧「本章角色 / 全书角色」能看到每个角色绑了哪个音色；
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
│   ├── analysis/extract/chapter_XXXX.json  整章提取原始结果（每句 + 说话人 + 对白情绪，LLM 直出）
│   ├── analysis/lines/chapter_XXXX.jsonl   逐句标注（说话人/情绪/语速；名字已映射成 role_id）
│   ├── voices/casting.json           角色 → 音色 + 推荐列表（含置信度与理由）
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
5. **情绪控制只开放 8 维向量**：逐句分析给主情绪 + 副情绪直接拼成引擎向量；文本描述通道（QwenEmotion）
   实现完整保留但默认关闭（`EMOTION_TEXT_ENABLED = False`），因为它常驻多占约 1.2 GB 显存、每句多约 1.8 秒。
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
