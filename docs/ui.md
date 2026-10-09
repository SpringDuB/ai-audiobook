# 浏览器界面

本地单用户界面：`uv run aiab serve` 之后打开 `http://127.0.0.1:8300`。
手机连同一个地址即可（`--host 0.0.0.0` 时用局域网 IP）。

**书页就是一个工作台**：左边章节、中间正文（原文 / 角色文本一键切换）、右边本章角色与音色。
进去之后不用再跳到别的页面，看分析、换音色、发起生成都在这一屏里完成。

## 1. 页面结构

| 路由 | 页面 | 做什么 |
|---|---|---|
| `#/shelf` | 书架 | 导入 txt / epub、看每本书进度（已分析/已生成/时长/异常）；卡片本身可点进书页，卡上只留「打开 / 删除」 |
| `#/book/{id}` | 书 · 工作台 | 三栏工作台：章节目录 + 正文/角色文本 + 角色音色 |
| `#/book/{id}/chapter/{n}` | 书 · 工作台（定位到第 n 章） | 同一个工作台，只是把选中的章节换成 n |
| `#/jobs` | 任务中心 | 运行中（进度条）/排队/失败，可取消与重试 |
| `#/book/{id}/issues` | 异常清单 | 降级与失败记录，按类型筛选、批量重试 |
| `#/voices` | 音色库 | 试听 + 上传新音色（参考音频 + 标签介绍）+ 停用/启用（停用的收进"已停用"栏） |
| `#/listen` | 听书 | 选一本有产物的书；手机优先，可「添加到主屏幕」当 App 用 |
| `#/listen/{id}` | 听书 · 章节 | 章节列表 + 离线下载（每章 / 整本 / 清空）；显示已离线数量与存储占用 |
| `#/listen/{id}/{n}` | 听书 · 播放 | 边听边高亮当前句、点句跳读、倍速、15 秒快进退、睡眠定时、自动连播；锁屏控制（Media Session） |
| `#/settings` | 设置 | LLM + **一键启动 TTS 服务**；其余参数用固定默认值 |

默认路径只有三步：**导入 → 分析台词（说话人 + 情绪）→ 生成音频 → 导出成品**，中途没有强制确认；所有人工操作都是事后修正。

### 工作台三栏

| 区域 | 内容 |
|---|---|
| 顶栏 | 书名单行显示（过长省略号，不会换行）+ 步骤条（① 分析台词 → ② 生成音频 → ③ 导出成品，右下角是这本书的任务进度，走 SSE）+ 四个常驻按钮：分析本章台词 / 生成本章音频 / 分析全本台词 / 生成整本音频（主按钮）；导出整本成品、打开成果文件夹、重新拼接本章、异常清单收进「更多 ▾」菜单。每个按钮都有悬浮说明（鼠标停上去看它到底动什么） |
| 左栏 | 全部章节（序号、标题、句数、时长、状态点）。点一下即切换，不整页刷新（地址栏同步 `#/book/{id}/chapter/{n}`，刷新页面还停在原章） |
| 中栏 | 页签「原文 / 角色文本」，**默认停在「原文」**（进书先看干净原文，逐句标注要点一下才出来）。角色文本整章连续，每句一行：行号、角色方块、说话人（按角色固定配色，旁白中性灰）、情绪/时长标签、台词、`试听 / 编辑 / 重生成`；原文页签直接显示这一章的干净文本 |
| 中栏 · 合成期 | 整章合成时逐句实时更新：**正在跑的那句挂转圈沙漏（生成中）**，还没轮到的是灰色「排队中」，音频一落盘这行立刻变成可点的「试听」+ 章节标题的「已合成 x/y」同步跳动，全程不用刷新页面（前端每秒拉一次逐句状态，只重画变化的那一行，编辑中的行不碰） |
| 右栏 | 页签「本章角色 / 全书角色」。每行是 角色名 + 状态（设计音色 / 未试听 / 试听过期）+ 出现范围（本章句数 / 全书句数与章节数）+ **可编辑的音色描述** + `试听 / 保存描述 / 让模型微调 / 换一版音色 / 绑库存音色` |

「分析本章台词」只重跑当前这一章的提取（角色表还没建时会先补一个临时角色表），适合单章返工；
「分析全本台词」弹窗勾选章节后：只勾一部分 = 一个批量任务（内部按大模型并发同时提取，最多 `AB_LLM_CONCURRENCY` 章并行，只动勾选的章，
新称呼并进现有角色表）；勾满全部章节 = 走整本任务（分章 → 提取 → 角色整合 → 音色描述，跨章合并同人异名更准）。**切句、说话人归属、逐句表演全部由大模型直出**：
模型逐句给出 `{"text","role","voice"}`（`"X：台词"` 会剥掉前缀归到 X），voice 是"这一句怎么说"（语气/语速/音量/气息，15 字内）；
随后再由大模型把"同一个人被叫了好几个名字"合并成一个角色，先出一张**选角表**（给角色定音色原型，
强制两两之间在音区/质地/年龄感上至少两项不同，避免同类角色撞成同一个声音；
只让有戏份的角色（台词数 ≥ `AB_CAST_SHEET_MIN_LINES`，默认 4）参与，按台词数排序、每批
`AB_CAST_SHEET_BATCH` 个（默认 16），后一批看得到前面已占用的原型，所以 600+ 角色的书也不会一次塞爆提示词），
再按原型为每个角色写一段**基础音色描述**（性别年龄感/音区/音色质地/咬字）。
基础描述只管"这个声音是什么样"，语速、音量、气息、情绪全部由逐句表演描述负责（两边不重叠，所以不会互相打架）。
出场极少的龙套不参与选角表，但会拿到"主角群已占用的音色"清单，别撞主角。
合成时两段拼成一句交给 Qwen3-TTS 的 VoiceDesign —— 音色由角色锁定，语气由本句决定。
已分析过的书再点一次会二次确认，确认后整本重跑
（会覆盖逐句标注，含人工修改），也可以只点「分析本章」逐章重跑。
「生成整本音频 / 生成本章音频」才会真正开始合成；「重新拼接本章」不重跑 TTS，只重拼音频与字幕。
「导出整本成品」跑完会提示一声，之后「更多 ▾ → 打开成果文件夹」可用（在系统文件管理器里打开 `output/`）。

逐句标注**不再产出"受话人"**：早期版本的 `addressee` 字段误判率高、对合成也没用，现已停止产出（老数据里的字段保留）。

每个角色行上是一段**可编辑的音色描述**（大模型按角色台词写，写得不满意可以自己改，
或让模型「微调」/「换一版音色」）：

- **试听**：按当前描述生成一段试听音频（该角色的试音台词），描述没改过就直接放缓存文件；
- **让模型微调**：把当前描述当锚点重写一版 —— 音区、质地、性别年龄感保持不变，只补缺失维度、
  删掉与逐句描述打架的语速/情绪、修掉自相矛盾；描述没变过就基本原样输出；
- **换一版音色**：弹窗里写一段你想要的音色（年龄感/音区/质地/气质，也可以写"说话慢一点"这种，
  模型会翻译成音色层面的写法），模型按这段要求重新设计这个角色的音色描述；不填就是直接重掷。
  换完这个角色已生成的音频全部重跑；
- **保存描述**：写进这本书的选角文件（`description_source=manual`，重跑分析不会被覆盖）。描述一改，
  这个角色的旧音频在下次生成时按新描述重跑（缓存键里带描述指纹，只有他一个人重跑）；
- **试听过期**：描述改过但试听还是旧的那一版，界面上直接标出来；
- **绑库存音色**：备用通道 —— 打开**音色悬浮窗**（搜索 + 性别/年龄/用途筛选 + 试听），选一个库存音色后
  这个角色改走参考音频克隆（`voice_source=library`），并提示哪些章节的成品因此失效。

### 音色库

- **上传新音色**：音色名称 + 参考音频（wav / mp3 / m4a / flac / ogg，5–15 秒干净人声最好，超过 60 秒会被拒）
  + 性别 / 年龄 / 语速 / 用途 / 标签 / 介绍；服务端统一转成单声道 16bit WAV 收进 `data/voices/<id>/`，
  于是合成、试听、选角、大模型推荐全都直接可用。
- **停用 / 启用**：停用是软删除 —— 音色不再出现在音色库主列表、选音色悬浮窗与大模型的音色库清单里，
  `ref.wav` 与 `voice.json` 原样保留，点「启用」立刻恢复。已绑定该音色的角色会在工作台标红提示。

### 设置页

只留两块，其余都藏起来：

1. **大模型（LLM）**：端点、模型、温度、并发 + 密钥状态（密钥只从 `.env` 读，不回显）。
2. **TTS 服务**：一个「一键启动 TTS 服务」按钮 + 停止 + 状态点（未运行 / 启动中 / 运行中）+ 推理后端（IndextTS-2.5）、
   模型来源（local / modelscope / huggingface）、模型目录、端口，外加可展开的实时日志。
   没有"假引擎"这类选项：合成只有真合成一条路。

情绪控制当前只开放 **8 维向量**：提取阶段给人物的每句话术标主情绪 + 副情绪及权重，直接拼成引擎的 8 维情感向量；
旁白不带情绪向量（只有人物话术需要情绪）。
「文本描述」通道（给每句配一句"怎么演"，由 QwenEmotion 翻译成向量）**实现已保留但暂时关闭**——
它常驻多占约 1.2GB 显存、每句多约 1.8 秒（实测），重新开放只需把 `EMOTION_TEXT_ENABLED` 改成 `True`，
设置页的下拉与启动参数会自动跟着出现。

点启动会：拉起独立进程（`tts/` 子项目，与后端不共进程）→ **先按模型来源下载/校验权重、再加载模型**（进度直接进日志面板）→
自动把合成引擎切到 `http://127.0.0.1:<port>` → 写入 `data/settings.json`。
**worker 每轮任务前重读设置**，所以改并发、改端点、换引擎、刚启动 TTS，都不用重启 worker。

`ffmpeg` 由项目自带（`uv sync` 时装好），响度归一与停顿、导出格式都已按有声书场景固定成默认值——
这些参数不再出现在界面上；确实要改的话改 `.env`（见 [export.md](export.md)）。

## 2. 快捷键（中栏句子区）

先点一下句子区域（或任意一句）让它获得焦点：

| 键 | 作用 |
|---|---|
| `J` / `K` | 下一句 / 上一句（当前句有强调蓝底色与行号高亮） |
| `空格` | 试听当前句 |
| `Enter` | 编辑当前句 |
| `Esc` | 退出编辑 |

## 3. 设计系统

方向：**制书台 · 纸与墨**，双主题。基础层是「纸」（浅色），`:root[data-theme="dark"]` 覆盖成「墨」（深色）；
首访跟随系统偏好，顶栏右侧一键切换，选择存 `localStorage`（`index.html` 里有防闪烁内联脚本，在样式表之前落主题）。

**强调色是校样蓝**（编辑部改稿的蓝铅笔，不参与印刷，正好对应 AI 标注是「中间稿」）：主操作、当前项、进度都用它；
**朱砂红只留给异常与破坏性操作**，这样「生成音频」和「异常 352 处」不会再撞成同一个颜色。

| 角色 | 纸（浅色） | 墨（深色） | 用途 |
|---|---|---|---|
| `--bg` | `#f7f4ef` | `#12100e` | 页面底 |
| `--surface` / `--surface-2` | `#fcfaf7` / `#f1ede6` | `#1a1714` / `#221e1a` | 面板 / 悬停与次级面 |
| `--border` / `--border-input` | `#e4ddd2` / `#93897a` | `#2e2823` / `#6b6255` | 分隔线 / 表单控件描边 |
| `--text` / `--text-muted` / `--text-faint` | `#1b1815` / `#5a544b` / `#736c60` | `#f3eee6` / `#b4aca0` / `#8c8377` | 正文 / 次要 / 辅助 |
| `--accent` | `#2c4e9a` | `#93b4f5` | 主操作、当前项、进度 |
| `--danger` | `#b23a28` | `#e4735c` | 异常、删除 |
| `--ok` / `--warn` | `#2f6b4f` / `#8a5a11` | `#6fbf95` / `#e0a852` | 完成 / 等待与降级 |

字体：标题与台词用 `Noto Serif SC`（本机已装），界面用 `Noto Sans SC`，数字、章节号、时长、置信度用 `Cascadia Mono` + `tabular-nums`。
圆角只有三档：控件 6px、面板 10px、药丸标签全圆。间距走 4 / 8 / 12 / 16 / 24 / 32 / 48 的梯度。

对比度（用 WCAG 相对亮度公式实测）：正文对页面底 16.1:1（深色 16.4:1），次要文字 6.8:1（8.5:1），
辅助文字 4.7:1（5.1:1），输入框描边 3.1:1（3.2:1），主按钮文字 7.8:1（8.8:1）。

没有紫蓝渐变、玻璃拟态、超大圆角卡片墙；颜色只在 `theme.css` 的 token 层出现，`app.css` 里 0 个裸色值（静态测试断言）。

**图标**：来自 [Lucide](https://lucide.dev)（ISC 许可）的现成图标集，取了 28 个按需内联在 `js/icons.js` 里，
线宽 1.75、颜色跟随 `currentColor`，不引 CDN、不引雪碧图；左侧导航、顶栏主流程、台词工具条、
音色播放条、设置页按钮都用它，图标按钮一律带 `aria-label` 或可见文字。

**品牌标识**：顶栏左侧用 `logo.png`（1024×1024 原图，保留在 `web/` 里当母版）。网页实际加载的是脚本生成的派生文件：
`logo-mark.png`（256×256，抠掉原图里烤进去的棋盘格底，真透明），**顶栏标记与浏览器标签页图标（favicon）共用这一份**，
深色主题下垫一层 `--brand-chip` 浅色垫片保证深紫描边不糊。`favicon.png`（64×64，浅底）留作备用，页面不再引用。重新生成用本机 ffmpeg：

```bash
ffmpeg -y -i src/audiobook/web/logo.png -vf "colorkey=0xf8f8f8:0.08:0.0,scale=256:256:flags=lanczos,format=rgba" -frames:v 1 src/audiobook/web/logo-mark.png
ffmpeg -y -i src/audiobook/web/logo.png -f lavfi -i "color=c=0xfbfbfb:s=1024x1024" -filter_complex "[0:v]colorkey=0xf8f8f8:0.08:0.0[key];[1:v][key]overlay=format=auto,scale=64:64:flags=lanczos" -frames:v 1 src/audiobook/web/favicon.png
```

**记忆点**：书页顶栏的步骤条「① 分析台词 → ② 生成音频 → ③ 导出成品」，以及句首与角色行统一的姓氏方块与行号。

## 4. 后端接口

界面只用这些 JSON 接口（OpenAPI 文档在 `/docs`）：

```
GET    /api/books                              书架（含 stats）
POST   /api/books                              上传 txt（multipart）
DELETE /api/books/{id}                          删除整本（任务行 + 数据目录）
POST   /api/books/{id}/run                     按断点补跑（分析 + 合成都排）
POST   /api/books/{id}/analyze?force=          只推分析链（分章→整章分析→选角）；force=true 整本重跑
POST   /api/books/{id}/chapters/{n}/analyze    只重跑本章的整章分析（角色 + 逐句情感）
POST   /api/books/{id}/generate                只推合成链（合成→渲染→合本）
GET    /api/books/{id}/chapters                章节状态表
GET    /api/books/{id}/chapters/{n}/text       这一章原文与字数
GET    /api/books/{id}/chapters/{n}/lines      句子（含 seq 兜底、emotion/pause/audio_url）
GET    /api/books/{id}/lines/{lineId}/audio    逐句试听（wav）
PATCH  /api/books/{id}/lines/{lineId}          改台词/说话人/情绪/停顿
POST   /api/books/{id}/lines/{lineId}/resynth  只重生成这一句
POST   /api/books/{id}/chapters/{n}/render     强制重渲染本章
POST   /api/books/{id}/export                  导出整本
GET    /api/books/{id}/output                  导出产物清单（有没有产物、里面有哪些文件）
POST   /api/books/{id}/output/reveal           在系统文件管理器里打开 output/
GET    /api/books/{id}/issues                  异常清单
POST   /api/books/{id}/issues/retry            批量重试
GET    /api/jobs · POST /api/jobs/{id}/cancel · POST /api/jobs/{id}/retry
GET    /api/voices                             音色（含分类标签、用途、描述、样本地址）
POST   /api/voices                             上传新音色（参考音频 + 名称/标签/介绍）
PATCH  /api/voices/{id}                        停用 / 启用（disabled 软删除，音频保留）
GET    /api/voices/{id}/sample
GET    /api/books/{id}/casting                 角色音色（含基础音色描述、试音台词、试听状态、章节数与句数）
PUT    /api/books/{id}/casting/{roleId}        把角色绑到库存音色（走克隆备用通道）
PUT    /api/books/{id}/roles/{roleId}/description   保存手改的音色描述
POST   /api/books/{id}/roles/{roleId}/rewrite       让大模型重写一版描述（异步任务）
POST   /api/books/{id}/roles/{roleId}/preview       按当前描述生成角色试听
GET    /api/books/{id}/roles/{roleId}/preview.wav   角色试听音频
GET    /api/books/{id}/listen                       听书目录（只列有 wav+srt 的章节、时长、离线体积估算）
GET    /api/books/{id}/chapters/{n}/subtitles       听书时间轴（逐句 start/end/文本/说话人）
POST   /api/books/{id}/chapters/{n}/mobile          wav → m4a 懒转码（已最新则秒回），离线下载前先调它
GET    /api/books/{id}/chapters/{n}/audio.m4a       手机播放用的 m4a（支持 Range 206，可拖动进度）
GET    /sw.js · GET /manifest.webmanifest           PWA（离线外壳 + 添加到主屏幕）
GET    /api/settings · PUT /api/settings       （密钥永不回显）
GET    /api/tts/local                          本机 TTS 状态 + 启动参数
POST   /api/tts/local/start · /stop            一键启动 / 停止
GET    /api/tts/local/logs?offset=N            增量拉日志
GET    /api/events                              SSE：任务快照
```

写入类操作都不会在请求里做重活：改句子只落盘 + 作废该章成品；`resynth` / `render` / `export` 只入队，由 `worker` 执行。

删除是唯一一个直接动文件的写入接口：它删掉 `data/books/{id}/` 整目录、`books` 行与该书所有 `jobs` 行。
**这本书还有任务在跑时会返回 409**（提示先去任务中心取消），避免 worker 在目录被删后继续写入留下无主残骸；
租约已过期的 `running` 残骸不算"在跑"，不会把删除永久卡住。

## 5. 人工修改后发生了什么

1. 改一句 → `lines/chapter_XXXX.jsonl` 更新（`emotion.source=manual`、记录 `edited_at`），该章 `render.json` 与容器被删除；
2. 点「重生成」→ 入队 `synthesize_line`（只重算这一行，其余行的音频不动）→ 完成后自动入队该章 `post`；
3. `post` 重渲染该章（停顿/响度按当前设置重算），并**作废整本 `book.wav/srt/mkv`**；
4. 再点「导出整本成品」或 `aiab run` → 自动入队 `book_export` 重建整本。
5. 改一个角色的音色描述（手改或让模型重写）→ **下一次生成**时这个角色的行按新描述重跑
   （缓存键里带"角色描述指纹 + 本句表演描述"），别的角色、别的句子继续走缓存；
   换成库存音色（绑库存音色）时，该角色出现过的所有章节成品会被作废，再点「生成本章/整本音频」按新音色重合成。

## 6. 界面冒烟测试（可重复执行）

**一条命令的自动化冒烟**（默认跳过，需要 Chrome + Node ≥ 22）：

```powershell
$env:AB_UI_SMOKE="1"; uv run pytest tests/test_ui_smoke.py -v
```

它在临时端口真起 `serve`，用 headless Chrome 校验：书架渲染出书、工作台渲染出章节列表/句子/角色栏、切到原文页签后句子归零而原文段落出现、
点「绑库存音色」能弹出音色窗并列出音色、角色行上有音色描述编辑框与试听/保存/重写按钮、
设置页有一键启动且不再有响度/ffmpeg 路径字段、390×844 下底部标签栏固定在底部，且 `consoleErrors` 为空。

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
- [x] **两种视图**：中栏页签「原文 / 角色文本」，默认「原文」；切换后一边的句子区/原文段落归零、另一边出现。
- [x] **音色可分类可选**：悬浮窗列出 96 个音色，性别/年龄/用途三行分类 + 搜索 + 试听。证据：[voice-picker.png](ui-shots/voice-picker.png)
- [x] **一键启动 TTS**：真点按钮 → 进程起来（`pid 56188`）→ 状态点变「运行中」→ 引擎自动切 `http`；再点「停止」后端口 8020 无监听、进程消失。证据：[settings-tts-running.png](ui-shots/settings-tts-running.png)（截图摄于还有 fake 后端的时期；进程托管逻辑与后端类型无关，现在只剩 indextts，并在启动时先下载/校验权重再加载）
- [x] **不再暴露底层旋钮**：设置页没有响度/停顿/ffmpeg 路径字段（冒烟测试用 `[data-key=loudness_mode]` / `[data-key=ffmpeg_path]` 断言为 `null`）。
- [x] **移动端可用**：390×844 下顶栏动作换行、章节列表可滚动、正文不截断、底部标签栏不盖住正文。证据：[workbench-mobile.png](ui-shots/workbench-mobile.png)
- [x] **每页三态**：`loadingState() / emptyState() / errorState()` 由 `ui.js` 的 `renderWithState()` 统一注入，所有视图都走这条路径。
- [x] **没有 AI 套路视觉**：CSS 里除 `:root` 的 token 外没有裸颜色值，无渐变、无玻璃拟态、无大圆角；字体全部本机命中，无外链（静态测试断言）。
- [x] **换页不留尾巴**：视图切走时 `runTeardowns()` 清掉定时器（设置页轮询）、共享 `Audio`、悬浮音色窗与 SSE 订阅。

### 实测记录

| 操作（全部在浏览器里点） | 结果 |
|---|---|
| 打开工作台 | 9 章列出，默认停在上次看的章节；中栏默认「原文」页签，直接显示该章干净文本 |
| 切「角色文本」 | 106 句逐行列出，行号 001–106 连续，原文段落隐藏；再切回「原文」句子区归零 |
| 切章 | 只重取该章数据，地址栏同步 `#/book/{id}/chapter/{n}`；返回新章后行号从 001 重新开始 |
| 点「换音色」 → 挑一个 → 试听 | 悬浮窗弹出、可用性别/年龄/用途筛选；试听走 `/api/voices/{id}/sample` |
| 保存音色 | 写进 `voices/casting.json`，toast 提示受影响的章节，右栏与左栏状态点同步更新 |
| 设置页点「一键启动 TTS 服务」 | 起来的是独立进程；`data/settings.json` 出现 `engine=http`、`tts_endpoints`、`tts_backend/model_*`；worker 无需重启 |
| 设置页点「停止」 | 进程树被清掉，端口释放，状态回到「未运行」 |
| 书架点「删除」 | 先弹二次确认（写明会一起删掉分析与音频）；确认后整本目录与任务行一起消失，服务端有任务在跑时则拦下并提示 |
| 任务中心 | 四个统计格 + 列头列表（任务 / 对象 / 进度 / 状态 / 操作），失败行的错误信息独占一列；取消与重试照旧；证据：[jobs-desktop.png](ui-shots/jobs-desktop.png) |
| 异常清单 | 失败朱砂 / 降级赭黄左侧 3px 色条；筛选片带条数、批量重试在工具条右侧；证据：[issues-desktop.png](ui-shots/issues-desktop.png) |
| 音色库 | 左边拖放参考音频、右边填元数据；卡片带紧凑播放条（▶ + 进度 + 时长）；停用收进底部折叠区，可一键启用；证据：[voices-desktop.png](ui-shots/voices-desktop.png) |
| 设置 | 三段卡片（LLM / TTS / 高级）+ TTS 状态面板（状态药丸、地址、backend、pid、合成引擎）+ 折叠日志；证据：[settings-desktop.png](ui-shots/settings-desktop.png) |
| 主题切换 | 顶栏右上角按钮翻转 `data-theme` 并写入 `localStorage`；浅色证据：[shelf-light.png](ui-shots/shelf-light.png) |
| 长书名不换行 | 顶栏书名单行省略号；1440 与 390 下 `--bar-h` 分别实测写回 75px / 142px（两行布局）；证据：[workbench-desktop.png](ui-shots/workbench-desktop.png)、[workbench-mobile.png](ui-shots/workbench-mobile.png) |
| 打开成果文件夹 | 没有产物时菜单项禁用（title 提示先导出）；`output/` 里出现 `book.wav` 后按钮可点，`POST /api/books/{id}/output/reveal` 交给系统文件管理器打开 |
| 角色配色 | 逐句行按角色 id 取模分配 8 组配色（`data-hue`），旁白固定中性灰；同一角色跨章颜色稳定 |

## 8. 已知限制

1. 工作台没有内联的"让 LLM 重新标注这一句"；人工修正走逐句编辑 + 规则重算。
2. 音色悬浮窗一次拉全量音色（96 个），不做分页——本地单用户够用。
3. 手机端是响应式网页，没有 PWA/离线缓存。
