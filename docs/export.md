# 产物导出（M3）

把 M2 合成的逐句音频变成"能听、能播、能上手机"的成品：停顿落位、响度归一、章节与整本 `wav/srt/mkv` 导出。

## 1. 产物在哪

全部在 `data/books/<bookId>/output/`：

| 文件 | 说明 |
|---|---|
| `chapter_XXXX.wav` | 单章成品音频：统一采样率、单声道 16-bit、按设置归一响度 |
| `chapter_XXXX.srt` | 单章字幕，时间轴由真实音频时长累计（含插入的停顿） |
| `chapter_XXXX.mkv` / `.mp4` | 音频 + 内嵌**软字幕**；mkv 用 `-c:a copy`（秒级），mp4 用 AAC + mov_text |
| `chapter_XXXX.render.json` | 渲染指纹与统计（幂等依据） |
| `book.wav` / `book.srt` / `book.mkv` | 整本合本；mkv 带章节标记，可直接跳章 |
| `playlist.m3u` | 章节顺序播放列表 |
| `book_章节.txt` | 章节时间点清单（人可读） |
| `merge-report.txt` | 导出报告（章节明细 + 输出文件清单） |

依赖：**不需要你装 ffmpeg** —— `uv sync` 时项目就带上了静态构建（`imageio-ffmpeg` 依赖），
解析顺序是 `AB_FFMPEG_PATH`（可选覆盖）→ 项目自带 → PATH。`ffprobe` 是可选的：
没有它时导出校验改用 `ffmpeg -i` 的输出解析，结果一样。

## 2. 常用命令

```powershell
# 全自动：分析 → 合成 → 章节渲染 → 整本导出
uv run aiab run <bookId>
uv run aiab worker

# 只导出（书已经合成完）
uv run aiab export <bookId>                              # 章节 mkv + 整本 wav/srt/mkv
uv run aiab export <bookId> --mode chapter --force       # 只重做章节容器
uv run aiab export <bookId> --mode book --no-container   # 只出 wav/srt，不封装容器
uv run aiab export <bookId> --container mp4              # mp4（相册/剪映友好）
uv run aiab export <bookId> --chapters 1,2,30-38 --out-dir D:\out
uv run aiab export <bookId> --dry-run                    # 只看会写哪些文件，不落盘
```

`--mode`：`chapter`（只章节容器）/ `book`（只整本）/ `all`（默认，两者都做）。
`--chapters` 支持 `1,2,30-38`、`1..10`。指定 `--out-dir` 时所有产物写到该目录，`output/` 里的规范产物保持不变。

## 3. 什么时候重编码，什么时候跳过

- **逐句音频 → 章节 wav**：由 `post` 任务调用渲染器。渲染指纹 `chapter_XXXX.render.json:render_key` 由「设置 + 每个片段的 id/停顿/时长/采样率/文件时间戳」算出；指纹一致且产物齐备时**直接复用**，不重编码。改一句台词、改停顿设置、换响度档位都会让指纹变化 → 只重做该章。
- **章节 wav → 章节容器**：比较容器与 `chapter_*.wav/.srt` 的 mtime，输入没变就跳过；`--force` 强制重做。
- **整本合本**：每次 `export` 的 `book` 模式都会重新拼接（纯文件拼接，很快），保证与当前章节产物一致。

## 4. 响度档位怎么选

| 档位 | 设置 | 用途 |
|---|---|---|
| `lufs`（默认） | `AB_LOUDNESS_MODE=lufs`、`AB_LOUDNESS_TARGET_LUFS=-16`、`AB_LOUDNESS_TRUE_PEAK=-1.5` | 手机/耳机通用，动态保留较好 |
| `rms` | `AB_LOUDNESS_MODE=rms`、`AB_LOUDNESS_RMS_TARGET_DB=-18 .. -23` | 对齐有声书平台标准；增益会被真峰值上限压住，不会削顶 |
| `off` | `AB_LOUDNESS_MODE=off` | 草稿：只拼接不归一（也不调用 ffmpeg）；单元测试默认用这一档 |

LUFS 走 ffmpeg `loudnorm` **两遍**（先测后归，`linear=true`），时长不受影响；RMS 档用 `volumedetect` 测均值后施加静态增益，同样不改时长。

## 5. 混采样率：为什么必须重采样

不同引擎/不同机器产出的片段采样率可能不同（22.05kHz / 24kHz / 48kHz）。**直接按帧拼接不会报错，但会静默变速变调**：24kHz 与 22.05kHz 各 1 秒拼在一起，会得到 1.91875 秒的音频，第二段被当成 24kHz 播放（频率整体升高 8.8%）。这条实测证据被锁在 `tests/test_render_mix.py::test_naive_frame_concat_of_mixed_rates_is_corrupt`。

导出时的处理：目标采样率 = `AB_EXPORT_TARGET_SAMPLE_RATE`，为 0 时自动取各片段最高采样率；不一致的片段先由 ffmpeg 显式重采样（并统一为单声道 16-bit），再拼接。章节与整本都走同一条规则。

### 停顿归一

停顿在导出时按「文本标点 + 句级情绪强度」重算，再套用 `AB_PAUSE_SCALE` 缩放并夹到 `AB_PAUSE_MIN_MS ~ AB_PAUSE_MAX_MS`
（句号 300 / 逗号 120 / 省略号 800 / 强度 ≥0.8 追加 150ms）；章节末尾额外静音用 `AB_PAUSE_TAIL_MS`（默认 0，即沿用最后一句自身的停顿）。

没有"场景切换停顿"这种概念：场景切分已经从流水线里去掉了，句子之间只按标点和情绪留白。

## 6. 手机播放建议

- **MKV**（默认，秒级产出）：VLC for Android/iOS、MX Player、MPV、nPlayer 都能直接读内嵌软字幕；`book.mkv` 还带章节标记，可跳章。缺点是系统「相册/音乐」类应用一般不索引 mkv。
- **MP4**（`--container mp4`）：AAC + mov_text 软字幕，iOS 相册、剪映、大多数手机自带播放器都能识别；代价是要重编码（比 mkv 慢）。
- 字幕是**软字幕**：播放器里要打开字幕显示（VLC 默认开，MX Player 需选字幕轨）；关掉字幕就是纯音频。
- 只想要纯音频：`--no-container` 只出 `book.wav` / `chapter_*.wav` 与 srt，wav 无损但体积大，可自行转 mp3/m4a。

## 7. 排错

- `找不到 ffmpeg：项目自带的依赖缺失` → 先 `uv sync`；想指定自己的构建再设 `AB_FFMPEG_PATH`。
- 字幕与音频不同步 → 看 `chapter_XXXX.render.json` 的 `warnings` 与 `issues.jsonl` 里的 `render_duration_mismatch`；同时确认 `audio_missing`（缺片段）的情况。
- `第 N 章缺少 wav/srt，已跳过` → 该章还没跑完 `post`；先 `uv run aiab run <bookId>` + `uv run aiab worker`。
