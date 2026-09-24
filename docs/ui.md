# 浏览器界面（M4）

本地单用户界面：`uv run aiab serve` 之后打开 `http://127.0.0.1:8300`。手机连同一个地址即可（`--host 0.0.0.0` 时可用局域网 IP）。

## 1. 页面结构

主线三层 **书架 → 书 → 章（场景 + 句子）**，加四个辅助页：

| 路由 | 页面 | 做什么 |
|---|---|---|
| `#/shelf` | 书架 | 导入 txt、看每本书的进度（已分析/已生成/时长/异常）、一键分析或生成 |
| `#/book/{id}` | 书 | 章节表（字数/场景/句子/时长/状态）、导出成品、异常入口 |
| `#/book/{id}/chapter/{n}` | 章 · 校对台 | 场景栏与场景概览、逐句试听/编辑/单句重生成 |
| `#/jobs` | 任务中心 | 运行中（进度条）/排队/失败，可取消与重试 |
| `#/book/{id}/issues` | 异常清单 | 降级与失败记录，按类型筛选、批量重试 |
| `#/voices` | 音色库 | 音色试听 + 角色→音色矩阵 |
| `#/settings` | 设置 | 模型 / TTS / 响度 / 停顿，写进 `data/settings.json` |

默认路径只有三个动作：**导入 → 一键分析 → 一键生成**，中途没有强制确认；所有人工操作都是事后修正。

## 2. 快捷键（校对台）

| 键 | 作用 |
|---|---|
| `J` / `K` | 下一句 / 上一句（当前句有朱砂引线高亮） |
| `空格` | 播放当前句 |
| `Enter` | 编辑当前句 |
| `Esc` | 退出编辑 |
| `Tab` | 焦点环可见（`:focus-visible`） |

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

**记忆点**：句子行左侧的**钤印 + 校样引线** —— 说话人是一个朱砂描边小方印（旁白用纸白弱化版），当前句有一条 1px 朱砂引线指向文本，像校样上的批注。

没有紫蓝渐变、玻璃拟态、超大圆角卡片墙；颜色/间距/字阶/阴影全部来自 `theme.css` 的 CSS 变量，视图样式里不写死颜色。

## 4. 后端接口

界面只用这些 JSON 接口（OpenAPI 文档在 `/docs`）：

```
GET    /api/books                              书架（含 stats）
POST   /api/books                              上传 txt（multipart）
POST   /api/books/{id}/run                     按断点补排队列
GET    /api/books/{id}/chapters                章节状态表
GET    /api/books/{id}/chapters/{n}/scenes     场景 + 参与者 + 时长
GET    /api/books/{id}/chapters/{n}/lines      句子（含 emotion/pause/audio_url）
GET    /api/books/{id}/lines/{lineId}/audio    逐句试听（wav）
PATCH  /api/books/{id}/lines/{lineId}          改台词/说话人/情绪/停顿
POST   /api/books/{id}/lines/{lineId}/resynth  只重生成这一句
POST   /api/books/{id}/chapters/{n}/render     强制重渲染本章
POST   /api/books/{id}/export                  导出整本
GET    /api/books/{id}/issues                  异常清单
POST   /api/books/{id}/issues/retry            批量重试
GET    /api/jobs · POST /api/jobs/{id}/cancel · POST /api/jobs/{id}/retry
GET    /api/voices · GET /api/voices/{id}/sample
GET    /api/books/{id}/casting · PUT /api/books/{id}/casting/{roleId}
GET    /api/settings · PUT /api/settings       （密钥永不回显）
GET    /api/events                              SSE：任务快照
```

写入类操作都不会在请求里做重活：改句子只落盘 + 作废该章成品；`resynth` / `render` / `export` 只入队，由 `worker` 执行。

## 5. 人工修改后发生了什么

1. 改一句 → `lines/chapter_XXXX.jsonl` 更新（`emotion.source=manual`、记录 `edited_at`），该章 `render.json` 与容器被删除；
2. 点「重生成这句」→ 入队 `synthesize_line`（只重算这一行，其余行的音频不动）→ 完成后自动入队该章 `post`；
3. `post` 重渲染该章（停顿/响度按当前设置重算），并**作废整本 `book.wav/srt/mkv`**；
4. 再点「导出成品」或 `aiab run` → 自动入队 `book_export` 重建整本。

## 6. 界面冒烟测试（可重复执行）

**一条命令的自动化冒烟**（默认跳过，需要 Chrome + Node ≥ 22）：

```powershell
$env:AB_UI_SMOKE="1"; uv run pytest tests/test_ui_smoke.py -v
```

它会在临时端口真起 `serve`，用 headless Chrome 校验：书架渲染出书、章页渲染出句子与钤印、设置页四组表单齐全、390×844 下底部标签栏固定在底部（不是铺满屏幕），且 `consoleErrors` 为空。

**手工探针**（要逐页截图或做交互时用）：

`tools/ui_probe.mjs` 用 Chrome DevTools Protocol 打开页面、等 JS 渲染完、输出关键结构与 console 错误并截图（Chrome 与 Node ≥ 22 是前提，本机已具备）：

```powershell
uv run aiab serve --port 8300           # 另开一个窗口
node tools/ui_probe.mjs "http://127.0.0.1:8300/#/shelf" "$env:TEMP\ui\shelf" 1440 900 --viewport
node tools/ui_probe.mjs "http://127.0.0.1:8300/#/book/<bookId>/chapter/1" "$env:TEMP\ui\chap" 390 844 --viewport --scroll-to=.proof
```

常用开关：`--viewport`（只截视口）、`--scroll-to=<选择器>`、`--click=<选择器>`（可重复）、`--steps=<json 文件>`（`click`/`eval`/`wait` 序列）、`--eval=<js>`（把求值结果附在输出里）。输出的 `consoleErrors` 为空才算通过。

## 7. 设计自检（2026-09-24 实测）

实测环境：Chrome headless 1440×900（桌面）与 390×844（移动），真实书 `0e6864037ff74d7f9f919939fb11d56a`（9 章 / 822 句 / 33:19）。

- [x] **移动端与桌面端都可用**：桌面 1440×900 与移动 390×844 各页渲染通过，`consoleErrors: []`。证据：[shelf-desktop.png](ui-shots/shelf-desktop.png)、[shelf-mobile.png](ui-shots/shelf-mobile.png)、[chapter-mobile.png](ui-shots/chapter-mobile.png)
- [x] **文字不溢出容器**：句子在 390px 宽下换行不截断；章节表在窄屏用 `.table-wrap` 横向滚动。实测移动端修掉一处真问题——底部标签栏曾因 `inset-block-start` 未覆盖而拉到 788px 高并盖住正文。
- [x] **四态齐全**：`.btn:hover / :active / [disabled] / :focus-visible`、`.scene-chip[aria-pressed]`、`.chip[aria-pressed]`、`.proof-line.is-current` 都在 `theme.css` 中定义并有可见反馈。
- [x] **每页三态**：`loadingState() / emptyState() / errorState()` 由 `ui.js` 的 `renderWithState()` 统一注入，所有视图都走这条路径。
- [x] **没有 AI 套路视觉**：CSS 里除 `:root` 的 token 外没有裸颜色值（`Select-String '#[0-9a-fA-F]{3,6}' src/audiobook/web/*.css` 只在 `:root` 命中），无渐变、无玻璃拟态、无大圆角。
- [x] **字体合规**：标题 `Noto Serif SC`、正文 `Noto Sans SC`、数字 `Cascadia Mono`，全部本机命中，无外链字体（静态测试断言 CSS 里不含 `http://`、`Inter`、`Roboto`、`Arial`）。
- [x] **记忆点**：钤印 + 校样引线。证据：[chapter-desktop.png](ui-shots/chapter-desktop.png)
- [x] **关掉浏览器任务继续**：所有长动作只入队；实测在浏览器里改一句并重生成，关掉页面后 `worker` 仍按队列完成了 `synthesize_line` → `post` → `book_export`。

### 实测记录（M4 验收）

| 操作（全部在浏览器里点） | 结果 |
|---|---|
| 打开书架 | 9 章书显示"已分析 9/9 · 已生成 9/9 · 33:19"，无 console 错误 |
| 打开章页（第 1 章） | 105 句、7 个场景、113 个钤印，场景栏可过滤 |
| 点「编辑」改台词 → 保存 | toast「已保存；本章成品已失效」；磁盘上 `lines.jsonl` 更新、`chapter_0001.render.json` 与 `.mkv` 被删除 |
| 点「重生成这句」 | 入队 `synthesize_line`，toast 提示；`worker` 执行后**只有该行** wav 变化（邻居 mtime 不变），随后自动重渲染该章（275.04s / 105 条字幕 / lufs） |
| 点「重渲染本章」 | 入队 `post`，跳转任务中心；顶部状态显示「排队 1」；证据：[jobs-live-queued.png](ui-shots/jobs-live-queued.png) |
| 任务中心 | 运行中/排队/失败三分区，失败 9 条可重试；证据：[jobs-desktop.png](ui-shots/jobs-desktop.png) |
| 音色库 | 音色为空时有明确空态 + 角色→音色矩阵（当前只有 `default`）；证据：[voices-desktop.png](ui-shots/voices-desktop.png) |
| 设置页 | 21 个字段分四组，密钥只显示"已配置"，保存写 `data/settings.json`；证据：[settings-desktop.png](ui-shots/settings-desktop.png) |
| 异常清单 | 无降级记录时给出空态，并单列"失败任务"提示去任务中心 |

## 8. 已知限制

1. 音色库在 M6 迁移之前是空的，矩阵里只能选 `default`。
2. 设置页的改动写进 `data/settings.json`，**运行中的 worker 需要重启**才会用新设置跑新任务。
3. 界面不提供"改语气让 LLM 重新标注"——那属于后续能力；当前是手工改 + 规则重算。
4. 手机端目前是响应式网页，没有 PWA/离线缓存。
