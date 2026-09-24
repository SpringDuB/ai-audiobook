# 迁移（M6）

把旧系统 `D:\workspace\datavrapCastV1.1.0` 的资产搬进新系统：96 个内置音色、样本书《地球最后一个修仙者》，以及单本书的 JSON 快照。

迁移**只读旧系统**，所有写入都发生在新项目的 `data/` 下；重复执行是幂等的。

## 1. 音色库（96 → `data/voices/`）

```powershell
uv run aiab migrate voices --source "D:\workspace\datavrapCastV1.1.0\tts\test\参考音频和文字"
uv run aiab migrate voices --source "..." --dry-run     # 只看会写什么
```

每个旧目录 → `data/voices/<voiceId>/`：

| 旧文件 | 新位置 |
|---|---|
| `参考音频.wav` | `ref.wav`（原样复制，不改编码） |
| `标签.json` | `voice.json` 的规范字段（`ageGroup→age_group`、`speechRate→speech_rate`…） |
| `试听文本.txt` / `情绪基调.txt` / `提示词.txt` / `应用场景.txt` | `voice.json` 的 `ref_text` / `mood_hint` / `prompt` / `scenes` |

约定与细节：

1. **`voiceId` 是排序序号**（`v001`…`v096`，按源目录名排序）——确定性、可复现；`voice.json.source.dir` 保留原始目录名便于追溯。
2. 缺 `标签.json` 的音色（`霸道总裁` → `v094`）**不阻塞**：从提示词里的"男声/女声"推断性别，其余字段留空并标 `needs_review: true`。
3. 只有 wav 的子目录才算音色；没有 wav 的目录会被列进 `skipped`。
4. 迁移后可被自动选角直接读取（`analysis/casting.py` 读 snake_case，兼容 camelCase），TTS 侧用 `data/voices/<id>/ref.wav` 上传参考音频。

实测（2026-09-24）：96 个音色、参考音频合计 61.1 MB、待补标签 1 个、缺参考音频 0 个；性别分布 男 45 / 女 48 / 中性 3。

## 2. 书籍迁移

```powershell
uv run aiab migrate book "<旧目录>\original.txt" --title "地球最后一个修仙者" `
  --legacy "<旧目录>" --cover "<旧目录>\cover.png"
```

做四件事：调用既有导入链（清洗 + 分章，**同步完成**，不等 worker）→ 复制封面到 `cover.png` 并写进 `book.json` → 把旧 `chapters.json` 与 `roles_*.json` 复制到 `legacy/` 留档 → 写 `migration.json`（源文件 sha256、分章对照、旧件清单）。

### 与旧系统对照

分章对照**先按标题精确匹配，再按"去掉（求收藏）这类括号后缀"的标题匹配，最后才按位置配对**，避免整体错位：

```
旧 10 章 → 新 9 章，标题命中 9，字符差 -3218，仅旧系统有：前言
```

`前言` 是旧系统保留的推广页（"更多免费网盘资源…"），新系统的清洗规则把它当广告丢掉——这正是分章规则差异里唯一的一处，其余章节按标题一一对上，字符差来自广告、水印与空行清理。

### 角色标注对照（回归基线）

分析跑完后：

```powershell
uv run aiab compare <bookId> --legacy "<旧目录>"
```

把旧 `roles_*.json` 的逐句说话人当基线，按"去标点后的句子文本"与新系统 `lines/*.jsonl` 对齐：

```
旧标注 846 句，匹配上 812 句，其中一致 673 句
一致率：82.9%
  不一致 139 句：旧旁白→新角色 139，旧角色→新旁白 0
```

**结论**：139 处不一致全部是"旧系统含糊标成旁白、新系统认出了真正的说话人"（例：`小张起来了，吃点什么。` 旧=旁白 → 新=老刘；`我靠，卫东，你跑哪去了…` 旧=旁白 → 新=王胖子），反向 0 句 —— 新系统在对白归属上没有丢过旧系统的信息，只多认了 139 句。

## 3. 单本书 JSON 快照

```powershell
uv run aiab snapshot export <bookId>                      # 默认 data/snapshots/<bookId>-<时间>.zip
uv run aiab snapshot export <bookId> --out D:\bak\book.zip
uv run aiab snapshot import D:\bak\book.zip [--force] [--book-id 新id]
```

快照只装 JSON/JSONL（`book.json`、`chapters.json`、`analysis/**`、`voices/casting.json`、`issues.jsonl`、`migration.json`…），**不含音频**，所以体积小、适合跨机传递"分析成果"。导入时会逐个校验 sha256；目标书已存在时默认拒绝，`--force` 才覆盖；`--book-id` 可以把同一份分析成果克隆成另一本书。

实测：一本书 863 个 JSON 文件导出 → 以新 id 导入 → `book.json` / `casting.json` 的 id 都已改写，章节数与句子数一致。

## 4. 迁移后的整链路

迁移只是起点：音色库就位后，选角会给角色分配真实音色，合成时会把这些 `ref.wav` 上传给 TTS 服务。实测（fake TTS 后端，本机无 GPU）：

| 步骤 | 结果 |
|---|---|
| `aiab migrate voices` | 96 音色、61.1 MB、1 个待补标签 |
| `aiab migrate book ...` | 新 bookId `2a48641c…`，9 章、封面与 `legacy/` 就位、分章对照写入 `migration.json` |
| `aiab run` + `worker` | 38 个任务全绿（角色/场景/逐句/选角） |
| `aiab compare` | 一致率 82.9%，差异全是"旁白→角色"的改进 |
| 合成 + 渲染 + 导出 | 818 句用 **14 个不同音色**发声（旁白 v014 ×665、张卫东 v001 ×71、王胖子 v058 ×28…），9 个章节 mkv + `book.wav/srt/mkv` |
| `aiab snapshot export/import` | 863 个 JSON 往返成功 |

## 5. 已知限制

1. 参考音频**没有重编码**：旧库的 wav 采样率/声道各不相同（IndexTTS 会自行处理），如果将来要求统一格式，用 `ffmpeg` 批量转一遍即可。
2. `needs_review: true` 的音色只有一个（`霸道总裁`），补 `data/voices/v094/voice.json` 里的字段后把它改成 `false` 就算完成。
3. 真实音质与显存表现要在 GPU 机器上验：按 `docs/tts-deploy.md` 起真机服务，跑一章后按 `docs/export.md` 复测响度。
4. 快照不含音频，还原后需要重跑合成（音频有缓存键，换机器会重新生成）。
