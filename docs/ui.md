# 浏览器界面

本地单用户界面：`uv run aiab serve` 之后打开 `http://127.0.0.1:8300`。
手机连同一个地址即可（`--host 0.0.0.0` 时用局域网 IP）。

**书页就是一个工作台**：左边章节、中间正文（原文 / 角色文本一键切换）、右边本章角色与音色。
进去之后不用再跳到别的页面，看分析、换音色、发起生成都在这一屏里完成。

## 1. 页面结构

| 路由 | 页面 | 做什么 |
|---|---|---|
| `#/shelf` | 书架 | 导入 txt、看每本书进度（已分析/已生成/时长/异常）、一键分析或生成 |
| `#/book/{id}` | 书 · 工作台 | 三栏工作台：章节目录 + 正文/角色文本 + 角色音色 |
| `#/book/{id}/chapter/{n}` | 书 · 工作台（定位到第 n 章） | 同一个工作台，只是把选中的章节换成 n |
| `#/jobs` | 任务中心 | 运行中（进度条）/排队/失败，可取消与重试 |
| `#/book/{id}/issues` | 异常清单 | 降级与失败记录，按类型筛选、批量重试 |
| `#/voices` | 音色库 | 96 个内置音色试听 + 角色→音色矩阵 |
| `#/settings` | 设置 | LLM + **一键启动 TTS 服务**；其余参数用固定默认值 |

默认路径只有三个动作：**导入 → 分析角色文本 → 生成有声书**，中途没有强制确认；所有人工操作都是事后修正。

### 工作台三栏

| 区域 | 内容 |
|---|---|
| 顶栏 | 书名 + 四个动作：分析角色文本 / 生成有声书 / 导出成品 / 重渲染本章 / 异常；右侧是跟这本书相关的任务进度（走 SSE） |
| 左栏 | 全部章节（序号、标题、句数、时长、状态点）。点一下即切换，不整页刷新（地址栏同步 `#/book/{id}/chapter/{n}`，刷新页面还停在原章） |
| 中栏 | 页签「角色文本 / 原文」。角色文本按**场景分段**，每句一行：行号、钤印、说话人（→ 受话人）、情绪/停顿/时长标签、台词、`▶ 试听 / 编辑 / 重生成`；原文页签直接显示这一章的干净文本 |
| 右栏 | 页签「本章角色 / 全书角色」。每行是 角色名 + 当前音色 + 出现范围（本章句数 / 全书句数与章节数）+「换音色」 |

「换音色」弹出**音色悬浮窗**：搜索框 + 性别 / 年龄 / 用途三行分类筹码 + 96 个音色卡片（标签、描述、试听、点选）。
选完立刻写进这本书的选角文件，并提示哪些章节的成品因此失效。

### 设置页

只留两块，其余都藏起来：

1. **大模型（LLM）**：端点、模型、温度、并发 + 密钥状态（密钥只从 `.env` 读，不回显）。
2. **TTS 服务**：一个「一键启动 TTS 服务」按钮 + 停止 + 状态点（未运行 / 启动中 / 运行中）+ 推理后端（fake / indextts）、
   模型来源（local / modelscope / huggingface）、模型目录、端口，外加可展开的实时日志。

点启动会：拉起独立进程（`tts/` 子项目，与后端不共进程）→ 自动把合成引擎切到 `http://127.0.0.1:<port>` → 写入 `data/settings.json`。
**worker 每轮任务前重读设置**，所以改并发、改端点、换引擎、刚启动 TTS，都不用重启 worker。

`ffmpeg` 由项目自带（`uv sync` 时装好），响度归一与停顿、导出格式都已按有声书场景固定成默认值——
这些参数不再出现在界面上；确实要改的话改 `.env`（见 [export.md](export.md)）。

## 2. 快捷键（中栏句子区）

先点一下句子区域（或任意一句）让它获得焦点：

| 键 | 作用 |
|---|---|
| `J` / `K` | 下一句 / 上一句（当前句有朱砂底色与行号高亮） |
| `空格` | 试听当前句 |
| `Enter` | 编辑当前句 |
| `Esc` | 退出编辑 |

## 3. 设计系统

方向：**编辑台 / 活字印刷**。深色为默认主题，纸白与墨黑为主色，**唯一强调色是朱砂红**（当前项、进度、主操作、异常标记）。

| 角色 | 值 |
|---|---|
| 底（墨） | `--ink-950 #0b0a08` → `--ink-500 #3a3427` 五级明度 |
| 字（纸白） | `--paper-100 #f4efe4` / `--paper-300` / `--paper-500` |
| 强调（朱砂） | `--cinnabar-300/500/700` |
| 标题字体 | `Noto Serif SC`（本机已装；回退 思源宋体 / 宋体） |
| 界面字体 | `Noto Sans SC`（回退 思源黑体 / 微软雅黑） |
| 数字字体 | `Cascadia Mono`（回退 JetBrains Mono / Consolas） |
| 圆角 | 2–3px（铅字感，不用大圆角） |

**记忆点**：句子行左侧的**钤印 + 行号**，以及右栏角色行与悬浮音色窗里同一套钤印。

没有紫蓝渐变、玻璃拟态、超大圆角卡片墙；颜色/间距/字阶/阴影全部来自 `theme.css` 的 CSS 变量，视图样式里不写死颜色。

## 4. 后端接口

界面只用这些 JSON 接口（OpenAPI 文档在 `/docs`）：

```
GET    /api/books                              书架（含 stats）
POST   /api/books                              上传 txt（multipart）
POST   /api/books/{id}/run                     按断点补跑（分析 + 合成都排）
POST   /api/books/{id}/analyze                 只推分析链（分章→角色→场景→逐句→选角）
POST   /api/books/{id}/generate                只推合成链（合成→渲染→合本）
GET    /api/books/{id}/chapters                章节状态表
GET    /api/books/{id}/chapters/{n}/text       这一章原文与字数
GET    /api/books/{id}/chapters/{n}/scenes     场景 + 参与者 + 时长
GET    /api/books/{id}/chapters/{n}/lines      句子（含 seq 兜底、emotion/pause/audio_url）
GET    /api/books/{id}/lines/{lineId}/audio    逐句试听（wav）
PATCH  /api/books/{id}/lines/{lineId}          改台词/说话人/情绪/停顿
POST   /api/books/{id}/lines/{lineId}/resynth  只重生成这一句
POST   /api/books/{id}/chapters/{n}/render     强制重渲染本章
POST   /api/books/{id}/export                  导出整本
GET    /api/books/{id}/issues                  异常清单
POST   /api/books/{id}/issues/retry            批量重试
GET    /api/jobs · POST /api/jobs/{id}/cancel · POST /api/jobs/{id}/retry
GET    /api/voices                             音色（含分类标签、用途、描述、样本地址）
GET    /api/voices/{id}/sample
GET    /api/books/{id}/casting                 选角（含每个角色的章节数与句数）
PUT    /api/books/{id}/casting/{roleId}        换音色（角色分析里有、选角落下的也能补）
GET    /api/settings · PUT /api/settings       （密钥永不回显）
GET    /api/tts/local                          本机 TTS 状态 + 启动参数
POST   /api/tts/local/start · /stop            一键启动 / 停止
GET    /api/tts/local/logs?offset=N            增量拉日志
GET    /api/events                              SSE：任务快照
```

写入类操作都不会在请求里做重活：改句子只落盘 + 作废该章成品；`resynth` / `render` / `export` 只入队，由 `worker` 执行。

## 5. 人工修改后发生了什么

1. 改一句 → `lines/chapter_XXXX.jsonl` 更新（`emotion.source=manual`、记录 `edited_at`），该章 `render.json` 与容器被删除；
2. 点「重生成」→ 入队 `synthesize_line`（只重算这一行，其余行的音频不动）→ 完成后自动入队该章 `post`；
3. `post` 重渲染该章（停顿/响度按当前设置重算），并**作废整本 `book.wav/srt/mkv`**；
4. 再点「导出成品」或 `aiab run` → 自动入队 `book_export` 重建整本。
5. 换一个角色的音色 → 该角色出现过的所有章节都被作废，右栏与章节列表的状态点会提示，重渲染后生效。

## 6. 界面冒烟测试（可重复执行）

**一条命令的自动化冒烟**（默认跳过，需要 Chrome + Node ≥ 22）：

```powershell
$env:AB_UI_SMOKE="1"; uv run pytest tests/test_ui_smoke.py -v
```

它在临时端口真起 `serve`，用 headless Chrome 校验：书架渲染出书、工作台渲染出章节列表/句子/角色栏、切到原文页签后句子归零而原文段落出现、
点「换音色」能弹出音色窗并列出音色、设置页有一键启动且不再有响度/ffmpeg 路径字段、390×844 下底部标签栏固定在底部，且 `consoleErrors` 为空。

**手工探针**（要逐页截图或做交互时用）：

```powershell
uv run aiab serve --port 8300           # 另开一个窗口
node tools/ui_probe.mjs "http://127.0.0.1:8300/#/shelf" "$env:TEMP\ui\shelf" 1440 900 --viewport
node tools/ui_probe.mjs "http://127.0.0.1:8300/#/book/<bookId>/chapter/1" "$env:TEMP\ui\wb" 390 844 --viewport
node tools/ui_probe.mjs "http://127.0.0.1:8300/#/book/<bookId>" "$env:TEMP\ui\picker" 1600 1000 --viewport --click=".cast-row .btn"
```

常用开关：`--viewport`（只截视口）、`--wait=<ms>`（等渲染的时长）、`--scroll-to=<选择器>`、`--click=<选择器>`（可重复）、
`--steps=<json 文件>`（`click`/`eval`/`wait` 序列）、`--eval=<js>`（把求值结果附在输出里）。输出的 `consoleErrors` 为空才算通过。

## 7. 设计自检（2026-09-24 实测）

实测环境：Chrome headless 1600×1000 / 1600×1100（桌面）与 390×844（移动），真实书 `2a48641cedee4e7e8bfaeece8012dfbb`
（9 章 / 818 句 / 整本 1547.72s，96 个内置音色）。

- [x] **一屏完成**：章节列表 9 项 + 章内 106 句 + 本章角色 4 / 全书角色 14 同屏，`consoleErrors: []`。证据：[workbench-desktop.png](ui-shots/workbench-desktop.png)
- [x] **两种视图**：中栏页签「角色文本 / 原文」切换后句子区归零、原文段落出现。
- [x] **音色可分类可选**：悬浮窗列出 96 个音色，性别/年龄/用途三行分类 + 搜索 + 试听。证据：[voice-picker.png](ui-shots/voice-picker.png)
- [x] **一键启动 TTS**：真点按钮 → 进程起来（`pid 56188`）→ 状态点变「运行中」→ 引擎自动切 `http`；再点「停止」后端口 8020 无监听、进程消失。证据：[settings-tts-running.png](ui-shots/settings-tts-running.png)
- [x] **不再暴露底层旋钮**：设置页没有响度/停顿/ffmpeg 路径字段（冒烟测试用 `[data-key=loudness_mode]` / `[data-key=ffmpeg_path]` 断言为 `null`）。
- [x] **移动端可用**：390×844 下顶栏动作换行、章节列表可滚动、正文不截断、底部标签栏不盖住正文。证据：[workbench-mobile.png](ui-shots/workbench-mobile.png)
- [x] **每页三态**：`loadingState() / emptyState() / errorState()` 由 `ui.js` 的 `renderWithState()` 统一注入，所有视图都走这条路径。
- [x] **没有 AI 套路视觉**：CSS 里除 `:root` 的 token 外没有裸颜色值，无渐变、无玻璃拟态、无大圆角；字体全部本机命中，无外链（静态测试断言）。
- [x] **换页不留尾巴**：视图切走时 `runTeardowns()` 清掉定时器（设置页轮询）、共享 `Audio`、悬浮音色窗与 SSE 订阅。

### 实测记录

| 操作（全部在浏览器里点） | 结果 |
|---|---|
| 打开工作台 | 9 章列出，默认停在上次看的章节；首章 106 句、场景分隔、行号 001–106 连续 |
| 切章 | 只重取该章数据，地址栏同步 `#/book/{id}/chapter/{n}`；返回新章后行号从 001 重新开始 |
| 切「原文」 | 显示整章原文（带首行缩进），句子区隐藏 |
| 点「换音色」 → 挑一个 → 试听 | 悬浮窗弹出、可用性别/年龄/用途筛选；试听走 `/api/voices/{id}/sample` |
| 保存音色 | 写进 `voices/casting.json`，toast 提示受影响的章节，右栏与左栏状态点同步更新 |
| 设置页点「一键启动 TTS 服务」 | 起来的是独立进程；`data/settings.json` 出现 `engine=http`、`tts_endpoints`、`tts_backend/model_*`；worker 无需重启 |
| 设置页点「停止」 | 进程树被清掉，端口释放，状态回到「未运行」 |
| 任务中心 / 异常清单 / 音色库 | 与 M4 一致，界面未改；证据：[jobs-desktop.png](ui-shots/jobs-desktop.png)、[issues-desktop.png](ui-shots/issues-desktop.png)、[voices-desktop.png](ui-shots/voices-desktop.png) |

## 8. 已知限制

1. 工作台没有内联的"让 LLM 重新标注这一句"；人工修正走逐句编辑 + 规则重算。
2. 音色悬浮窗一次拉全量音色（96 个），不做分页——本地单用户够用。
3. 手机端是响应式网页，没有 PWA/离线缓存。
