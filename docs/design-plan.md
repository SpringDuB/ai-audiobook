# AI 有声书 · 界面重设计方案

> 状态：方向已确认（A 纸与墨 / 主操作蓝 + 异常红 / 首访跟随系统）。按第 8 节的阶段落地。
> 可视化预览：[docs/design-preview.html](design-preview.html)（双击打开，右上角切换深浅主题）

---

## 0. 一页结论

1. **保留产品骨架**：书架 → 工作台三栏（章节 / 台词 / 角色）→ 任务 / 异常 / 音色 / 设置，信息架构不动。
2. **换成一套语义 token + 双主题**：`纸`（浅色）/ `墨`（深色），右上角一键切换，记住选择，首访跟随系统。
3. **强调色从朱砂红换成校样蓝**：红只留给「异常 / 删除」，主操作与报错不再撞色。
4. **修掉工作台顶栏换行**：书名单行省略号，动作区收敛成 3 主 + 1 更多，窄屏是显式的两行布局而不是靠折行碰运气。
5. **不做的事**：不引入框架、不引入 Tailwind、不引入 CDN 字体与图标库，`tests/test_api_static.py` 的离线约束继续成立。

---

## 1. 设计读数与旋钮

**Design read**：中文有声书生产工具，操作密集型多面板产品界面，用户是反复使用的个人创作者 / 小工作室；语言取「安静、精密、纸感」的编辑台气质；落地形式是原生 CSS 变量 + 双主题语义 token。

三个旋钮（沿用 design-taste-frontend 的 dial 体系）：

| 旋钮 | 取值 | 理由 |
| --- | --- | --- |
| `DESIGN_VARIANCE` | 4 | 这是工具不是落地页。工作台三栏、章节列表、台词列表都要求可预测，不需要不对称构图 |
| `MOTION_INTENSITY` | 3 | 高频操作界面，动效只做状态反馈：悬停、按压、开合、进度。不做入场编排与滚动叙事 |
| `VISUAL_DENSITY` | 7 | 一屏要同时看章节、台词、情绪、音色，紧凑优先；用 1px 分隔线而不是到处套卡片 |

**明确不套用的规则**：landing page 与作品集类的版式规则（hero、bento、marquee、scroll 叙事）不适用本产品；我们只取其中的设计工程部分：排版、颜色校准、动效纪律、双主题、可访问性与内容溢出处理。

---

## 2. 现状审计

审计对象是当前 `feat/ui-workspace` 分支的界面（截图见本方案评审时的 1440×900 实测）。

### 2.1 保留

- **信息架构**：左导航五项 + 书页三栏，路径清晰，不需要动。
- **CSS 契约**：`theme.css` 管 token，`app.css` 管布局，视图里不写死颜色。实测 `app.css` 里裸十六进制色值 **0 个**，只有 6 处 `rgba()` 需要 token 化（两处当前行渐变、两处遮罩、两处阴影）。重构成本低。
- **交互细节**：按钮已有 hover / active / disabled，破坏性操作有二次确认，键盘有跳转与 Esc 关闭。
- **文案语气**：短句、动词开头、中文原生（「装版中」「换音色」），保留。

### 2.2 退役

| 项 | 问题 |
| --- | --- |
| 活字印刷装饰 | `.letterpress` 的上下 text-shadow 压印、`.seal` 印章方块在浅色主题下会变成脏污噪点，且和「现代工具」的读数冲突 |
| 朱砂一色多用 | 主按钮、当前行、进度条、异常标签、删除按钮全是朱砂红，主操作与报错无法区分 |
| 卡片套卡片 | 书架 `sheet` + `book-card` 双层边框，视觉噪声高 |
| 到处并列标签 | 书卡上「第 9 章 · 已分析 9/9 · 已生成 1/9 · 13:20 · 异常 352 处」全平铺，扫描成本高 |
| 硬编码 sticky 偏移 | `.rail { inset-block: 56px 0 }`、`.workbench__bar { inset-block-start: 56px }`、`.script__head { inset-block-start: 116px }`（两处）写死了顶栏高度，任何一行高度变化都会错位 |

### 2.3 顶栏换行问题的根因

```css
/* app.css:251 当前实现 */
.workbench__bar {
  display: flex;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;          /* ← 换行的直接开关 */
}
.workbench__title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-inline-size: 0;       /* ← 只写了 min-width，没有截断规则 */
}
```

三个叠加因素：

1. 容器 `flex-wrap: wrap`，空间不够就整块折行，折的是**整个标题块**，不是标题内部。
2. `.workbench__title` 没有 `overflow: hidden` + `text-overflow: ellipsis` + `white-space: nowrap`，书名会把主轴撑开，把动作区挤到第二行。
3. 动作区有 7 个按钮加一条流程提示（`workspace.js:732` 一处，`workspace.js:329` 的 pending 视图还有一处），1440px 下已经挤满，宽度再少一点或书名再长一点就必然折行。

---

## 3. 视觉方向

### 3.1 方向 A（推荐）：制书台 · 纸与墨

暖中性色基底（纸白 / 墨黑，带一点黄褐调，和中文衬线正文字很搭）+ 一个强调色 + 极少量状态色。卡片圆角 10px，控件 6px，分隔线 1px，阴影只在浮层和悬停时出现。

选择它的理由：

- 和现有「墨 / 纸」命名、以及书架、书页这类阅读场景连续，用户不会觉得换了另一个产品；
- 暖中性色在低亮度下长时间看不刺眼，深色主题正好是当前用户的默认习惯；
- 衬线只出现在**书名与台词正文**，是「读物」该有的样子；界面骨架全部无衬线，保证密度与可读性。

### 3.2 方向 B（备选）：控制台 · 石墨与琥珀

冷灰蓝石墨底 + 琥珀色信号色，更接近录音室设备与开发者工具的读数，冷、硬、科技感强。代价是与「中文小说」的阅读场景割裂，长文阅读舒适度不如 A。

**建议**：走 A。如果你想要更强的「音频设备感」，我们只把信号色换成琥珀，其余不动，改动量约 30 行。

### 3.3 强调色决策：朱砂红 → 校样蓝

编辑部用蓝铅笔改稿，蓝线不参与印刷，天然是「中间稿」的隐喻，正好对应这个产品在做的事：AI 标注的台词与情绪都是可改的中间稿。

副作用是好的：**红被释放出来专管异常**。当前版本里「生成整本音频」（主操作）和「异常 352 处」（错误）是同一个红，扫描时极易误判。

---

## 4. Design tokens

全部写进 `theme.css`，命名分两层：底层色阶可留，组件只用语义层。深色为主题默认，浅色跟随系统或用户选择。

### 4.1 颜色

| Token | 深色（墨） | 浅色（纸） | 用途 |
| --- | --- | --- | --- |
| `--bg` | `#12100E` | `#F7F4EF` | 页面底色 |
| `--surface` | `#1A1714` | `#FCFAF7` | 顶栏 / 面板 / 卡片 |
| `--surface-2` | `#221E1A` | `#F1EDE6` | 悬停、次级面 |
| `--inset` | `#151311` | `#EFEAE2` | 输入框、代码块底 |
| `--border` | `#2E2823` | `#E4DDD2` | 分隔线 |
| `--border-strong` | `#3D362F` | `#D6CDBF` | 悬停描边、浮层边 |
| `--border-input` | `#6B6255` | `#93897A` | 表单控件描边（对底色 ≥ 3:1） |
| `--text` | `#F3EEE6` | `#1B1815` | 正文 |
| `--text-muted` | `#B4ACA0` | `#5A544B` | 次要文字 |
| `--text-faint` | `#8C8377` | `#736C60` | 辅助信息（仍 ≥ 4.5:1） |
| `--accent` | `#93B4F5` | `#2C4E9A` | 主操作、当前项、进度 |
| `--accent-hover` | `#A8C4FA` | `#25417F` | 主操作悬停 |
| `--accent-soft` | `#1C2436` | `#E7ECF8` | 选中行底色、强调标签底 |
| `--on-accent` | `#0F1420` | `#FFFDFA` | 强调色上的文字 |
| `--danger` | `#E4735C` | `#B23A28` | 异常、删除 |
| `--danger-soft` | `#2A1B17` | `#F8E9E5` | 异常标签底 |
| `--ok` | `#6FBF95` | `#2F6B4F` | 完成 |
| `--warn` | `#E0A852` | `#8A5A11` | 等待、降级提示 |
| `--scrim` | `rgba(6,5,4,.60)` | `rgba(30,24,18,.42)` | 弹窗遮罩 |
| `--focus` | 双环：底色 + 强调色 | 同左 | 焦点环 |

对比度实测（WCAG 相对亮度公式，脚本计算，非估算）：

| 组合 | 深色 | 浅色 |
| --- | --- | --- |
| 正文 / 页面底色 | 16.44:1 | 16.11:1 |
| 次要文字 / 面板 | 8.45:1 | 6.83:1 |
| 辅助文字 / 页面底色 | 5.09:1 | 4.73:1 |
| 强调色文字 / 页面底色 | 9.12:1 | 7.19:1 |
| 主按钮文字 / 主按钮底 | 8.84:1 | 7.77:1 |
| 异常色 / 页面底色 | 6.24:1 | 5.42:1 |
| 输入框描边 / 页面底色 | 3.17:1 | 3.14:1 |

不出现纯黑 `#000000` 与纯白 `#FFFFFF`；深色主题的所有面板都比底色亮 1 到 2 档，靠明度分层而不是靠阴影。

### 4.2 排版

| Token | 值 | 用途 |
| --- | --- | --- |
| `--font-sans` | `"Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif` | 界面骨架 |
| `--font-serif` | `"Noto Serif SC", "Source Han Serif SC", "Songti SC", "SimSun", serif` | 书名、台词正文 |
| `--font-mono` | `"Cascadia Mono", "JetBrains Mono", "Consolas", monospace` | 数字、字号、章节号、时间 |
| `--text-xs` | 12px | 标签、辅助 |
| `--text-sm` | 13px | 按钮、表格、次要在 |
| `--text-base` | 14px | 正文（深色 14px 观感约等于浅色 15px，浅色主题正文可上调到 15px） |
| `--text-lg` | 17px | 面板标题、书卡标题 |
| `--text-xl` | 22px | 页面标题 |
| `--text-2xl` | 30px | 书架大标题 |

规则：数字一律 mono + `tabular-nums`（章节号、时长、字数、置信度、情绪值对齐）；正文行高 1.65，台词行高 1.9；标题不用超大字号制造层级，用字重与颜色。

### 4.3 间距 / 圆角 / 阴影 / 层级

- 间距：4 / 8 / 12 / 16 / 24 / 32 / 48，继续沿用现有 `--space-*`。
- 圆角：**控件 6px、卡片与浮层 10px、状态点与药丸标签全圆角**。这是唯一一套圆角规则，全站不再出现第二种取值。
- 阴影：`--shadow-1`（悬停）与 `--shadow-2`（浮层）。深色主题用 `0 1px 0 rgba(0,0,0,.45)` 这类压边阴影，浅色主题用暖调低透明度阴影，不用纯黑。
- 层级：`10` 吸顶导航、`20` 顶栏、`30` 悬浮试听、`40` toast、`60` 音色选择器、`70` 模态。写进 token 注释，组件不再随手写 `z-index`。

### 4.4 动效

```css
--ease-out: cubic-bezier(0.23, 1, 0.32, 1);
```

| 场景 | 时长 | 动的东西 |
| --- | --- | --- |
| 按钮按压 | 90ms | `transform: scale(0.98)` |
| 悬停 / 描边 / 底色 | 140ms | 背景色、描边色、文字色 |
| 页签、展开、弹窗 | 160 到 220ms | `transform`、`opacity` |
| 进度条推进 | 200ms | `inline-size`（唯一的尺寸动画，且低频） |

纪律：不动 `top / left / width / height`（进度条除外）；不用 `transition: all`；`prefers-reduced-motion: reduce` 下全部压到 1ms；键盘触发的高频操作不加动画。

---

## 5. 关键界面改造

### 5.1 全局骨架

- **顶栏**（52px）：品牌块 + 面包屑（单行截断）+ 运行状态药丸 + 主题切换按钮。面包屑过长时截断，完整内容进 `title`。
- **左导航**（88px）：汉字图标在上、文字在下，当前项用 `--accent-soft` 底 + 强调色文字，去掉左侧朱砂竖条（在浅色主题下过重）。
- **吸顶高度变量化**：

```css
:root { --masthead-h: 52px; --bar-h: 56px; }
.rail            { inset-block: var(--masthead-h) 0; }
.workbench__bar  { inset-block-start: var(--masthead-h); }
.script__head    { inset-block-start: calc(var(--masthead-h) + var(--bar-h)); }
```

`--bar-h` 由 `workspace.js` 里的 `ResizeObserver` 实测后写回 `document.documentElement.style`，顶栏高度随字号或换行变化时不再错位。

### 5.2 书架

- 导入区收成一行：拖放区（虚线框）+ 书名输入 + 主按钮；epub 说明降级为 `field__hint`。
- 书卡：标题最多两行（`-webkit-line-clamp: 2` + 完整书名进 `title`），状态标签放右上角，统计信息单行，进度用 4px 细线 + `8/9` mono 数字。
- 动作只保留「打开 / 删除」，整卡可点（保留当前的可访问实现：`role="link"` + `tabIndex` + Enter / Space）。
- 进度色：进行中用 `--accent`，完成用 `--ok`，异常用 `--danger`。

### 5.3 工作台顶栏（本次必修）

**结构**：一行三块（书名区 / 流程步骤 / 动作区），书名区 `minmax(0, 1fr)`，动作区 `auto`，整体 `display: grid` 而不是 `flex-wrap`。

```css
.workbench__bar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-4);
  /* 关键：容器不再 flex-wrap，折行交给下面的断点显式决定 */
}
.workbench__title { min-inline-size: 0; display: flex; align-items: baseline; gap: var(--space-2); }
.workbench__book {
  min-inline-size: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;   /* 长书名永远单行，超出省略 */
}
.workbench__actions { display: flex; flex-wrap: nowrap; gap: var(--space-2); }

@media (max-width: 1000px) {
  .workbench__bar { grid-template-columns: minmax(0, 1fr); }  /* 显式两行：标题一行，动作一行 */
}
```

**动作区收敛**：可见按钮不超过 3 个。

| 位置 | 按钮 | 说明 |
| --- | --- | --- |
| 常驻 1 | 分析本章台词 | 次级按钮 |
| 常驻 2 | 生成本章音频 | 次级按钮 |
| 常驻 3 | 导出整本成品 | 主按钮（`btn--primary`） |
| 「更多 ▾」菜单 | 分析多个章节…、重新拼接本章、异常清单 | 收进下拉，保留现有 `title` 说明 |

**流程提示**：从动作区挪到书名下方的 `workbench__steps` 步骤条（三步，当前步高亮），1280px 以下隐藏，避免和高频按钮抢注意力。原来的 7 个按钮挤在一行的状态从此消失。

**pending 视图**（`workspace.js:329`）：复用同一套 `workbench__bar` 结构与截断规则，避免导入长书名时同样折行。

### 5.4 台词与角色面板

- 每句一行：左侧 mono 序号，中间是角色标签 + 台词正文（衬线 15px / 行高 1.9），下方是情绪与操作。
- 情绪：保留 8 维向量，但只把**最强的 2 个**维度显式画出来（`悲 0.72` + 34px 细条），其余收在悬浮提示里。现在的问题是把全部维度铺开，读不出重点。
- 旁白行不显示情绪，只在角色标签上做弱化区分。
- 角色面板：头像位用姓氏方块（衬线字），推荐音色改成药丸 + 独立试听按钮（保留现有 `/api/voices/{id}/sample` 逻辑），已选中的药丸用 `--accent-soft` 底；音色被停用时标红并给「换一个」直达。

### 5.5 任务中心

功能保持：刷新、取消（运行中 / 排队中）、重试（失败 / 已取消）、四组的空态提示、每行的任务号 / 类型 / 书 / 章节 / 进度 / 尝试次数 / 更新时间。

改造：

- 顶部四个统计格（运行中 / 排队中 / 失败 / 最近完成）给总量，配色只在「运行中」用强调色、「失败」用朱砂，其余中性。
- 列表加列头（任务 / 对象 / 进度 / 状态 / 操作），行改成五列网格。当前实现里进度、错误信息和动作挤在一个自适应格里，长错误会把同行挤变形。
- 失败行的错误信息用 `--danger`，并且只在自己那一列折行；尝试次数与时间进 mono 小字。
- 「最近完成」整行降到 `--text-muted`，不加动作按钮，视觉上退到背景层。
- 每组的空态保留原文案（「当前没有运行中的任务。」「队列是空的。」）。

### 5.6 异常清单

功能保持：书名选择器、去任务中心、失败任务提示条、分类筛选片（含条数）、批量重试（按当前筛选）、空态、每条的严重度 / 类型 / 章节 / 句号 / 原因 / 降级说明。

改造：

- 严重度用左侧 3px 色条区分：失败朱砂、降级赭黄。当前只用左边框颜色区分且行底色与页面接近，扫起来吃力。
- 筛选片改成药丸 chip，选中态用 `--accent-soft` + 强调色文字，条数用 mono。
- 降级说明单独做成缩进块（左侧 2px 线 + 次级底色），和失败原因拉开层级。
- 「另有 N 个失败任务」改成赭黄提示条，右侧直接给「去任务中心重试」。
- 批量重试常驻在工具条右侧，跟筛选片同一行，减少一次视线跳转。

### 5.7 音色库

功能保持：上传（名称、参考音频、性别、年龄、语速、用途、标签、介绍）、试听、停用、已停用区启用、TTS 设置入口、空态。

改造：

- 上传区从「8 个字段平铺三列」改成两栏：左边是参考音频拖放区（第一等公民），右边是元数据两列网格，介绍独占整行。当前参考音频和「标签」同级，用户容易忽略。
- 面板标题栏直接写清「参考音频 5 到 15 秒最稳」，把原本堆在字段下方的长提示前置。
- 音色卡：姓氏方块 + 名称 + 来源标签（内置 / 上传）+ 标签行 + 一句话介绍 + 紧凑播放条 + 底部 `v001` 与「停用」按钮。
- 播放条是按钮 + 细进度 + 时长的组合（隐藏原生 `<audio>`，由按钮驱动播放）。原生控件在不同浏览器里外观差异大，也和这套 token 不搭。
- 已停用区收成页面底部的折叠面板（`<details>`），行内保留启用按钮与音色 id。
- 可选后续（本次不做，属于新功能）：音色多了以后加搜索与标签筛选。

### 5.8 设置

功能保持：保存设置、覆盖来源提示、LLM 四项字段与密钥状态、TTS 状态轮询、一键启动 / 停止、启动参数五项、运行日志（末 220 行）、高级折叠区（TTS 端点、合成并发、并发上限）与两段说明。

改造：

- 页面分三段卡片：大模型（LLM）、TTS 服务、高级（默认折叠）。当前三段是同样的 `sheet`，层级靠标题字号区分，弱。
- TTS 状态从一行散落的点 + 文字，收成一块状态面板：状态药丸（运行中 / 启动中 / 未运行）+ 地址 + backend + pid + 合成引擎，全部 mono 小字。
- 「一键启动 TTS 服务」用主按钮、「停止」用危险按钮并排，右侧补一句「点下去会发生什么」的说明，替代原先挂在字段下方的后端提示。
- 日志保留 `<details>` 折叠，内部换成等宽日志块（次级底色 + 11rem 高位滚动）。
- 字段网格：LLM 两列，TTS 启动参数三列，长文本（TTS 端点）独占整行。
- 「密钥已配置 / 未配置」改成状态标签，和说明文字同一行。

### 5.9 弹窗 / toast / 音色选择器

- 统一圆角 10px、描边 `--border-strong`、阴影 `--shadow-2`、遮罩 `--scrim`。
- 弹窗加 `overscroll-behavior: contain`，打开时焦点进主按钮，关闭时焦点回到触发元素（当前只做了 `confirm.focus()`）。
- toast 保持底部居中，成功 / 失败用文字标签区分，不只靠颜色。
- 音色选择器复用新 token，推荐项左侧改强调色细条（当前是朱砂，浅色主题下过重）。

---

## 6. 主题切换实现

```html
<!-- index.html -->
<meta name="color-scheme" content="light dark">
<script>
  // 在样式表之前执行，避免刷新闪一帧白
  (function () {
    try {
      var saved = localStorage.getItem("aiab-theme");
      var system = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
      document.documentElement.dataset.theme = saved === "light" || saved === "dark" ? saved : system;
    } catch (error) {
      document.documentElement.dataset.theme = "dark";
    }
  })();
</script>
```

```css
/* theme.css 结构 */
:root { /* 浅色 token 作为基础层 */ color-scheme: light; }
:root[data-theme="dark"] { /* 深色覆盖 */ color-scheme: dark; }
```

- 切换按钮：顶栏右侧 32px 图标按钮，`aria-label="切换到浅色主题"`，状态存 `localStorage['aiab-theme']`。
- `<meta name="theme-color">` 跟随主题更新（移动端地址栏配色）。
- 原生控件跟着 `color-scheme` 走，避免 Windows 下深色主题里的白色下拉框。
- **测试要同步改**：`tests/test_api_static.py::test_root_serves_app_shell` 现在断言 `<meta name="color-scheme" content="dark">`，改成 `light dark`；再加一条断言 `theme.css` 同时含浅色与 `[data-theme="dark"]` 两套 token。
- 视图里 6 处 `rgba()` 换成 token：两处当前行渐变用 `--accent-soft`，遮罩用 `--scrim`，阴影用 `--shadow-2`。改完 `app.css` 依然 0 个裸色值。

---

## 7. 可访问性与工程质量清单

按 Vercel Web Interface Guidelines 逐条过：

- 图标按钮必须有 `aria-label`（主题切换、试听按钮已具备，改造后保持）。
- 异步反馈容器带 `aria-live`（顶栏运行状态、toast 已有）。
- 所有可点区域是真的 `<button>` / `<a>`；书架卡片保留键盘 Enter / Space 打开。
- 焦点可见：`:focus-visible` 双环，浅色主题外环用页面底色，内环 `--accent`；禁止 `outline: none` 无替代。
- 文本溢出：标题类元素统一 `min-inline-size: 0` + 截断 + `title` 兜底；这是本次顶栏问题的同类根因，一次性扫一遍所有标题容器。
- 表单：每个输入都有 `<label>`；上传表单补 `name` 与 `autocomplete="off"`；错误信息就近显示。
- 触控：加 `touch-action: manipulation`；弹窗内 `overscroll-behavior: contain`。
- 只动画 `transform` / `opacity`；`prefers-reduced-motion` 降级；不写 `transition: all`。
- 大列表：台词超过 200 行时启用 `content-visibility: auto`（一章 1500 句的极端情况）。
- 中英数混排：数字用 `tabular-nums`；品牌名 `AI 有声书` 加 `translate="no"` 防止浏览器翻译破坏。

---

## 8. 落地计划

### P0 · token 层（不影响功能，先合）

1. `theme.css` 拆成「基础层 + `[data-theme="dark"]` 覆盖层」，写入第 4 节全部 token。
2. `app.css` 里 6 处 `rgba()` 换成 token，其余选择器改为语义 token 名。
3. `index.html` 加防闪烁内联脚本、`color-scheme` 改 `light dark`、顶栏加主题切换按钮（`js/app.js` 里 10 行逻辑）。
4. 更新 `tests/test_api_static.py` 断言；新增 `tests/test_ui_smoke.py` 用例：切换按钮存在、点击后 `documentElement.dataset.theme` 翻转、`localStorage` 写入、两种主题都无 console 错误。

### P1 · 布局与组件（本次诉求主体）

5. 顶栏换行修复：`.workbench__bar` 改 grid + 书名截断 + 动作区收敛为 3 主 + 更多菜单 + 步骤条（`app.css`、`workspace.js`）。
6. 吸顶高度变量化 + `ResizeObserver` 写回 `--bar-h`。
7. 书架、台词、角色面板、音色库、任务、异常、设置逐个套用新 token 与圆角 / 间距 / 层次规范。
8. 弹窗、toast、音色选择器统一到 `--scrim` / `--shadow-2` / 焦点管理。

### P2 · 打磨与验收

9. 空态 / 加载态 / 错误态：加载用骨架条替代「装版中…」，错误块给下一步动作。
10. 两种主题 × 1440 / 1024 / 390 三档宽度全部截图走查，移动端 `consoleErrors` 必须为空。
11. 更新 `docs/ui.md` 的界面小节与 `README.md` 的截图说明（如有）。

### 涉及文件

| 文件 | 改动 |
| --- | --- |
| `src/audiobook/web/theme.css` | token 重构（主要工作量） |
| `src/audiobook/web/app.css` | 6 处 rgba 换 token，顶栏 / 书架 / 台词 / 面板样式 |
| `src/audiobook/web/index.html` | 防闪烁脚本、`color-scheme`、主题切换按钮 |
| `src/audiobook/web/js/app.js` | 主题读写与切换 |
| `src/audiobook/web/js/views/workspace.js` | 顶栏结构、更多菜单、`--bar-h` 测量 |
| 其他视图 js | 只做类名与结构微调，不动数据逻辑 |
| `tests/test_api_static.py`、`tests/test_ui_smoke.py` | 断言与冒烟用例 |
| `docs/ui.md` | 设计小节更新 |

---

## 9. 验收标准

- [ ] 1440 / 1024 / 390 三档宽度下，超长书名的顶栏**不换行**，动作区不挤压（截图对比）。
- [ ] 深浅两套主题都能完整浏览全部页面，无硬编码浅色 / 深色残留（`app.css` 裸色值仍为 0）。
- [ ] 主题选择在刷新与新标签页后保持；首访跟随系统；无闪白。
- [ ] 关键对比度：正文 ≥ 7:1，次要 ≥ 4.5:1，输入框描边 ≥ 3:1（本方案已实测，改色后需复测）。
- [ ] 所有图标按钮有 `aria-label`，键盘可完成「切主题 / 打开书 / 选章节 / 确认弹窗」。
- [ ] 顶栏按钮文案与悬浮说明仍能让新用户看懂流程（三主按钮 + 更多菜单）。
- [ ] `uv run pytest -q` 与 `AB_UI_SMOKE=1` 冒烟全绿。

---

## 10. 已确认的决策

| 项 | 结论 |
| --- | --- |
| 视觉方向 | A 纸与墨（暖中性 + 校样蓝），B 石墨与琥珀不采用 |
| 颜色分工 | 主操作与当前项用校样蓝；异常、失败、删除保留朱砂红 |
| 默认主题 | 首访跟随系统，之后跟随用户在顶栏的选择，存 `localStorage` |
| 未被削减的功能 | 任务取消 / 重试、异常分类筛选与批量重试、音色上传 / 试听 / 停用 / 启用、设置保存与 TTS 启停日志全部保留 |

下一步从 P0 开始落地，每个阶段单独提交，方便逐段验收。
