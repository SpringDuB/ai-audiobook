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
| C3 一键启动 | 浏览器点「一键启动 TTS 服务」→ 真起独立进程（`pid 56188`，`backend=fake`），状态点「运行中」，`data/settings.json` 出现 `engine=http` / `tts_endpoints` / `tts_backend/model_source/model_dir/port`；点「停止」后 `Get-NetTCPConnection -LocalPort 8020` 为空、进程消失 |
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

遗留：

1. 索引页仍是"书架→书页"两步，卡片式书架未做视觉重排（本次只改书页）。
2. 悬浮音色窗一次拉全量音色，音色库上千时需要分页/虚拟滚动。
3. 真机音质与显存表现仍要在 GPU 机器上按 `docs/tts-deploy.md` 复测。
