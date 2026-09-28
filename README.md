# AI 有声书（ai-audiobook）

本地单用户的「小说 → 多角色情感有声书」流水线：导入 txt，自动分章、识别角色、逐句标注情感，
再按角色切换音色逐行合成，导出可播放的 `wav / srt / mkv`。

三个进程各干各的：**后端只做快请求，长任务全在 worker，TTS 单独部署**。
浏览器里只有一个工作台：左边章节、中间全文/角色文本、右边本章角色与音色。

## 流水线

```
导入 txt ─► 分章(本地，自动) ─► 提取台词与说话人(含对白情绪) ─► 角色整合(同人异名合并) ─► 推荐音色 ─► 合成 ─► 每章渲染 ─► 整本合本
                                 └──────────────「分析角色文本」──────────────┘          └────「生成有声书」────┘
```

- 导入后**只自动分章**（纯本地文本处理，不烧 LLM），分析和合成都必须用户点按钮；
- 台词切分、说话人归属、情绪标注**全由大模型直出**（没有规则分词/归属）：模型逐句给出 `旁白 / 角色名` 与对白情绪，`"X：台词"` 会被剥掉前缀归到 X；旁白不带情绪向量，只有人物话术带；
- **角色整合也走大模型**：把"本名/绰号/尊称"等同一个人的不同称呼合并成一个角色；
- **音色推荐还是大模型**：为每个角色从音色库里推荐 1–3 个音色（含置信度与理由），直接显示在角色行上，点一下即选中；用户不选就用第一条推荐；
- 「分析本章」可只重跑单章的整章分析，适合返工；已分析过的书再点「分析角色文本」会二次确认后整本重跑；
- 改一句台词 / 换一个音色 → 只作废受影响的章节成品，其余音频不动。

## 架构

| 进程 | 启动 | 端口 | 职责 |
|---|---|---|---|
| 后端 | `uv run aiab serve --port 8300` | 8300 | FastAPI + 静态 UI；写入只落盘和入队，不跑长任务 |
| worker | `uv run aiab worker` | — | 领 SQLite 队列：分章、台词提取、角色整合、音色推荐、合成、渲染、合本 |
| TTS | `uv run aiab tts start` | 8020 | IndexTTS-2.5 推理；独立 venv、独立进程，与后端不共进程 |

任务队列用 **SQLite（WAL + 租约）**，没有 Celery / Redis / 消息中间件；
进度判断只看文件（JSON / JSONL / WAV / SRT），所以中断后重跑天然断点续传。

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

前置：Python 3.13 + [uv]；TTS 需要一块 NVIDIA GPU（约 6 GB 显存）与 Python 3.11（index-tts 自己的要求）。

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

1. **书架**导入 txt → 自动分章；
2. 点「**分析角色文本**」等分析链跑完（台词提取 → 角色整合 → 音色推荐，每个角色默认用第一条推荐）；
3. 进工作台按需改台词、换音色、逐句试听/重生成；
4. 点「**生成有声书**」开始合成，每个章节生成 `wav + srt`，最后合出整本；
5. **导出成品** → `data/books/<bookId>/output/`：`chapter_XXXX.wav/.srt/.mkv`、`book.wav/.srt/.mkv`、`playlist.m3u`；
6. 播放：`mkv` 内嵌软字幕，手机用 VLC / nPlayer / MX Player 直接打开就能看字幕；`wav + srt` 同名放一起也可以；
7. **删除整本**：书架卡片上的「删除」（二次确认）。它会删掉这本书的数据目录、`books` 行与所有 `jobs` 行；
   如果这本书还有任务在跑，会返回提示让你先去任务中心取消，避免 worker 在目录被删后继续写入。

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run aiab serve --port 8300 [--host 0.0.0.0]` | 起后端 + 浏览器 UI（手机连局域网 IP 即可） |
| `uv run aiab worker [--once] [--max-jobs N]` | 起 worker；`--once` 只领一个任务，便于验收 |
| `uv run aiab import <book.txt> --title 书名` | 命令行导入并入队分章 |
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
│   ├── source/original.txt           原始书稿
│   ├── chapters.json                 分章结果
│   ├── analysis/characters.json      角色表（主名 + 别名 + 出现章数）
│   ├── analysis/extract/chapter_XXXX.json  整章提取原始结果（每句 + 说话人 + 对白情绪，LLM 直出）
│   ├── analysis/lines/chapter_XXXX.jsonl   逐句标注（说话人/情绪/语速；名字已映射成 role_id）
│   ├── voices/casting.json           角色 → 音色 + 推荐列表（含置信度与理由）
│   ├── audio/chapter_XXXX/<lineId>.wav     逐句音频（+ .meta.json 缓存键）
│   ├── output/                       成品 wav/srt/mkv、render.json、合本、播放列表
│   └── logs/llm.jsonl                LLM 调用日志
└── voices/<voiceId>/{voice.json,ref.wav}   音色库（迁移的 96 个内置音色）
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
| [docs/tts-deploy.md](docs/tts-deploy.md) | TTS 环境准备、模型三选一、验收清单 |
| [docs/export.md](docs/export.md) | 停顿 / 响度 / 容器导出与播放说明 |
| [docs/migrate.md](docs/migrate.md) | 旧系统音色与书籍迁移 |

## 约定

- 提交信息末尾带 `Co-authored-by: Codex <codex@openai.com>`。
- 国内网络慢时可用阿里云 PyPI 镜像：`$env:UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple"`（或 `uv add --default-index ...`）。

[uv]: https://docs.astral.sh/uv/
