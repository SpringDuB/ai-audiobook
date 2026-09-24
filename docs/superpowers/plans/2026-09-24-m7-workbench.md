# AI 有声书 M7 工作台 Implementation Plan

**Goal:** 把"书页"改成单页工作台（左章节 / 中原文·角色文本 / 右本章角色音色），
把 TTS 变成**一键启动**（ffmpeg 项目自带、响度与停顿不再暴露给用户），
并让运行中的 worker 能自己重读设置——改并发、换引擎、刚启动 TTS 都不用重启进程。

**Architecture:** 后端加三个能力：`tts_service.LocalTtsService`（托管 `tts/` 子进程 + 探活 + 日志增量读）、
`pipeline` 按阶段过滤（`analysis` / `audio`）、`render/ffmpeg` 的项目自带解析链与 `ffmpeg -i` 兜底。
前端把 `views/book.js` + `views/chapter.js` 合成 `views/workspace.js`，新增 `voicepicker.js` 悬浮音色窗，
`ui.js` 增加视图级 `onTeardown/runTeardowns`。

**Tech Stack:** Python 3.13 + uv（`imageio-ffmpeg` 提供静态 ffmpeg）；原生 ES 模块 + CSS 变量，无构建步骤、无框架、无 CDN。

**Spec:** `docs/superpowers/specs/2026-09-24-ai-audiobook-design.md`（§UI、§TTS 部署、§4.3 导出）

## Global Constraints

- TTS 仍然跑在**独立进程**（`tts/` 子项目），后端只托管，不把推理塞进 serve 进程。
- 界面**不暴露** ffmpeg 路径、响度、停顿这类专业参数；默认值写在 `config.py`，要改走 `.env`。
- 所有长任务只入队（`/analyze`、`/generate`、`/export`、`render`、`resynth`），请求里不做重活。
- 视图切走必须清干净：定时器、SSE 订阅、共享 `Audio`、悬浮窗。
- 每个 commit 的消息末尾追加一行：`Co-authored-by: Codex <codex@openai.com>`。
- 命令写成 PowerShell 可直接执行的形式。

## 本计划的范围

| 做 | 不做（归属） |
|---|---|
| 单页工作台（章节/正文/角色音色三栏）+ 悬浮音色窗 | 段落级拖拽排序、多角色批量替换 |
| 一键启动 / 停止 TTS + 实时日志 + 自动切引擎 | 在界面里装 CUDA/驱动、显存监控面板 |
| ffmpeg 项目自带 + ffprobe 可选兜底 | 打包成单文件 exe |
| worker 每轮重读设置 | 分布式多 worker 协调 |
| 修 `null` 行号、文档与冒烟测试 | 视频（M5） |

---

## 任务清单

- [x] T1 配置与依赖：`Settings` 增加 `tts_backend/tts_model_source/tts_model_dir/tts_hf_endpoint/tts_port` 并纳入 `OVERLAY_KEYS`；`uv add imageio-ffmpeg`（走阿里云镜像）。
- [x] T2 ffmpeg：`find_ffmpeg` 改为 `显式路径 → 项目自带 → PATH`；`find_ffprobe` 允许缺失；`probe_json` 在没有 ffprobe 时解析 `ffmpeg -i`（streams/chapters/format 子集）。
- [x] T3 `tts_service.py`：`LocalTtsService.{status,start,stop,logs,build_command}` + `data/tts-service.json` 状态文件 + Windows 进程树停止；`aiab tts start|stop|status|logs`。
- [x] T4 API：`/api/tts/local{,/start,/stop,/logs}`；`/api/books/{id}/analyze|generate`；`/chapters/{n}/text`；选角接口补 `chapters/lines` 并允许补建角色；行号 `seq/scene_index` 兜底。
- [x] T5 worker：`WorkerContext.refresh()` 每轮重读设置，按 `ENGINE_KEYS/LLM_KEYS` 决定是否重建引擎与 LLM 客户端。
- [x] T6 前端工作台：`views/workspace.js`（章节列表 + 正文/角色文本 + 角色音色 + 键盘 J/K/空格/Enter/Esc + SSE 进度）、`voicepicker.js`（搜索 + 性别/年龄/用途分类 + 试听）。
- [x] T7 设置页：只留 LLM 与「一键启动 TTS 服务」（状态点、日志、后端、模型来源/目录/端口），高级区折叠，删掉响度/停顿/ffmpeg 路径。
- [x] T8 测试与文档：`test_tts_service.py`、`test_api_workspace.py`、pipeline 分相、worker 重载；更新 `test_ui_smoke.py`/`test_api_static.py`/`ui_probe.mjs`；重拍截图；更新 `docs/ui.md`、`docs/tts-deploy.md`、`docs/export.md`。

---

## 验收记录

环境：本机 Windows + PowerShell，Python 3.13，Chrome headless，真实书 `2a48641cedee4e7e8bfaeece8012dfbb`（9 章 / 818 句 / 96 音色）。

| 项 | 结果 |
|---|---|
| C1 单元测试 | `uv run pytest` → **318 passed, 4 skipped**（比 M6 基线 298 净增 20 个用例：TTS 服务 7、工作台接口 5、worker 重载 3、ffmpeg 兜底 3、分相 2） |
| C2 浏览器冒烟 | `$env:AB_UI_SMOKE=1; uv run pytest tests/test_ui_smoke.py` → **4 passed**：书架、工作台（章节列表 9 / 句子 106 / 角色行 4）、原文页签、悬浮音色窗（96 行）、设置页无 `[data-key=loudness_mode]`、390×844 底部标签栏 |
| C3 一键启动 | 浏览器点「一键启动 TTS 服务」→ 真起独立进程（当时 `pid 56188`），状态点「运行中」，`data/settings.json` 出现 `engine=http` / `tts_endpoints` / `tts_backend/model_source/model_dir/port`；点「停止」后 `Get-NetTCPConnection -LocalPort 8020` 为空、进程消失。删除 fake 后端后，进程托管改用测试替身 + 注入命令继续覆盖（`tests/test_tts_service.py`） |
| C4 项目自带 ffmpeg | `find_ffmpeg()` → `.venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`（83.6 MB）；`-filters` 有 `loudnorm`、`-muxers` 有 `matroska`/`mp4`；把 ffprobe 屏蔽后 `probe_json` 仍能读出 `streams=[audio,subtitle]`、9 个章节标题与时长 |
| C5 工作台实测 | 首章 106 句一次渲染完，场景分隔正确，行号 001–106 连续（旧数据没有 `seq` 时由后端按行序兜底，不再出现 `null`）；切章只重取本章数据并把地址栏同步成 `#/book/{id}/chapter/{n}`；`consoleErrors: []` |
| C6 音色窗 | 96 个音色按 性别/年龄/用途 三行分类 + 搜索；试听走 `/api/voices/{id}/sample`；选中即写 `voices/casting.json` 并提示受影响章节 |
| C7 worker 免重启 | 单元测试覆盖：端点/引擎变化 → 下一轮任务前换引擎，重建失败保留旧引擎不崩；worker 启动日志不再要求"改完必须重启" |
| C8 真实音质 | ⛔ 需要 GPU 机器按 `docs/tts-deploy.md` 跑真机（与 M6 相同遗留） |

实跑修掉/发现的问题：

1. **`null` 行号**：老书 `lines.jsonl` 没有 `seq`/`scene_index`，前端 `String(undefined)` 渲染成 `null` → 后端按行序兜底，前端改成"本章第几句"连续编号（`seq` 是场景内序号，直接显示会一段一段重来）。
2. **`event.currentTarget` 在 `await` 后为 null**：一键启动按钮的 `finally` 想恢复按钮文本时报 `TypeError` → 先 `const button = event.currentTarget` 再 await（工作台的通用按钮同样处理）。
3. **`replaceChildren(...[cond ? node : null])` 会渲染出字面量 "null"**：设置页状态行先拼数组再 `.filter(Boolean)`。
4. **语法坑**：`cond ? ...spread : value` 不是合法 JS，工作台角色列表一开始整页白屏 → 改成 `...(cond ? arr : [fallback])`。
5. **依赖下载卡住**：`uv add` 默认拉 `files.pythonhosted.org` 超时 → 改用阿里云镜像（`UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/`）重新 lock，锁文件 URL 也指向镜像。
6. **移动端底部被标签栏盖住**：`#main[data-view=book]` 把 padding 归零后丢了底部留白 → 移动端补 `padding-block-end: 5.5rem`。
7. **fake 后端误导人**：选 fake 一键启动后什么都不下载、也不加载模型，用户以为服务坏了 → 产品里彻底删除 fake（主项目 `engines/factory` 只留 http、老 `AB_ENGINE=fake` 给明确报错；tts 只留 indextts），测试用一个 `StubBackend` 替身。
8. **启动不下载模型**：`IndexTtsBackend.load()` 原来直接读 `config.yaml`，从不调 `ensure_model` → 选 modelscope 也不会下载。改成"先 import 依赖 → 按来源 `ensure_model`（必要时下载，进度进日志）→ 加载"，并且 `serve` 默认预加载。
9. **假的 Python 版本门槛**：`backends/indextts.py` 里那道 "必须 3.10/3.11" 的守卫是我们自己写的，不是 IndexTTS 的要求（真正卡人的是 pynini 之类依赖有没有轮子）→ 直接删掉，装得上就能跑。
10. **日志乱码**：子进程 stdout 默认按 Windows 代码页（GBK）写，而读取端按 UTF-8 解码 → 界面上显示成一串 `�`。修法：给子进程强制 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` / `PYTHONUNBUFFERED=1`，读取端再补 utf-8 → gbk → replace 三级兜底（实测 `text.count("\ufffd") == 0`）。
11. **真后端依赖没写进 pyproject**：之前靠一串手工命令 → `tts/pyproject.toml` 增加 `indextts`（torch/torchaudio/transformers/librosa/soundfile/numpy/sentencepiece…）与 `download`（modelscope/huggingface_hub）两个 extra，基础依赖保持轻量；`tts/tests/test_packaging.py` 守住这几个包不许再丢。
12. **`uv run` 的自动同步会撞文件锁**：正在跑的服务锁着 venv 里的 `watchfiles/_rust_notify.pyd`，同步于是以 `failed to remove file ... os error 5` 失败，还会把 `aiab-tts` 的 editable 安装连根拔掉。改成直连 `tts/.venv` 的解释器跑 `-m aiab_tts`（并 `PYTHONPATH=tts/src`，临时安装坏了也能起），venv 缺基础依赖时才自动补一次 `uv sync`。
13. **孤儿服务越攒越多**：端到端用例用 `uv run ... python -m aiab_tts serve` 起服务，`terminate()` 只杀到 uv，真正的 python 子进程活了下来——一天下来攒了 52 个，全部锁着 venv 里的文件。修法：测试改成直连 venv 解释器 + `taskkill /T` 收进程树；产品启动时若发现端口上已经有人在应答但它不在台账里，直接在日志里点名提示。清理时把这 52 个（没有任何端口在监听的）孤儿杀掉了。
14. **Python 版本门槛的真相**：`<3.12` 是 `index-tts` 自己的 `pyproject.toml` 声明（`requires-python = ">=3.10,<3.12"`，附带 `torch==2.8.*` 等 pin），我们这侧已经没有任何版本检查；`tts` 的 `indextts` extra 按它这份 pin 对齐，文档也改成"真后端用 3.11 建 venv"。
15. **界面上的模型来源被忽略**：界面发的是 `tts_model_source`，`/api/tts/local/start` 只读 `model_source`，于是永远回退到默认的 `local`（选了 modelscope 也没用）→ 接口两种键名都认，并加了"按设置页真实 payload 调用"的回归用例。
16. **模型目录解析到项目根**：子进程 `cwd` 是仓库根，`model_dir="checkpoints"` 于是成了 `ai-audiobook\checkpoints`（报 `FileNotFoundError: ...\ai-audiobook\checkpoints\config.yaml`）→ 改成以 `tts/` 为工作目录（`tts/checkpoints`、`tts/data`、`tts/.env` 都跟着对上了），启动日志里也直接打印解析后的绝对路径。
17. **自己的日志看不到**：uvicorn 只配它自己的 logger，我们 `logging.info("模型就绪…")` 全被丢掉 → `serve` 里 `logging.basicConfig`，顺手加了一条"torch 看不到 CUDA（CPU 版）"的显式警告与装 CUDA 版的命令（Windows 上 PyPI 的 torch 是 `+cpu`，会静默退回 CPU 推理）。

遗留：

1. 索引页仍是"书架→书页"两步，卡片式书架未做视觉重排（本次只改书页）。
2. 悬浮音色窗一次拉全量音色，音色库上千时需要分页/虚拟滚动。
3. 真机音质与显存表现仍要在 GPU 机器上按 `docs/tts-deploy.md` 复测。
