# AI 有声书 M6 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把旧系统的资产搬进新系统并留下可核对的证据：96 个内置音色 → `data/voices/<voiceId>/`；样本书《地球最后一个修仙者》→ 新系统的分章/分析/合成；旧 `chapters.json` 与 `roles_*.json` 作为回归对照基线；另提供单本书的 JSON 快照导出/导入。

**Architecture:** 新增 `src/audiobook/migrate/`（`voices.py` 音色迁移、`books.py` 书籍迁移、`compare.py` 与旧系统对照、`snapshot.py` 快照）。全部是纯函数 + 文件操作，不依赖 LLM/TTS；CLI 挂 `aiab migrate ...` 与 `aiab snapshot ...`。迁移**幂等**：重复执行结果一致，已存在的产物按内容覆盖。

**Tech Stack:** Python 3.13（标准库：`json`/`shutil`/`hashlib`/`zipfile`/`wave`）+ 既有 `store`/`importer`/`analysis`；测试用 pytest + 临时假源目录（不碰真实旧系统文件）。

**Spec:** `docs/superpowers/specs/2026-09-24-ai-audiobook-design.md` §10（音色库/书籍/工具化）、§5.4（选角打分读 `voice.json`）、§4.1（目录）、§12.6（迁移测试）

## Global Constraints

- 迁移脚本**只读旧系统**（`D:\workspace\datavrapCastV1.1.0`），一切写入都发生在新项目的 `data/` 下。
- `voiceId` 必须**确定性**：`v001`…`v096`，按源目录名排序；重复迁移得到同一套 id（选角结果才可复现）。
- `voice.json` 用 snake_case 规范字段（`age_group`/`speech_rate`/`voice_quality`/`language_style`/`usage_type`），同时保留 `source` 与原始标签块便于追溯；缺 `标签.json` 的音色（`霸道总裁`）不阻塞，标 `needs_review: true`。
- 书籍迁移**不直接复用**旧 `roles_*.json`：只作为对照基线与仓库里的 `legacy/` 副本。
- 快照只装 JSON/JSONL（不含 wav），导入时默认拒绝覆盖已存在的书（`--force` 才覆盖）。
- 每个 commit 的消息末尾追加一行：`Co-authored-by: Codex <codex@openai.com>`。
- 命令写成 PowerShell 可直接执行的形式。

## 本计划的范围

| 做 | 不做（归属） |
|---|---|
| 96 音色迁移 + 试听 + 参与自动选角 | 新录音色的界面（M4 已有；本计划只填数据） |
| 样本书迁移 + 分章/标注对照报告 | 真实 IndexTTS 音质评估（需要 GPU，出清单给用户） |
| 单本书 JSON 快照导出/导入 | 全库备份、增量同步 |
| 迁移后跑通：导入 → 分析 → 合成 → 导出 | 视频（M5） |

---

## 冻结契约

### 迁移产物

```
data/voices/<voiceId>/
  voice.json          规范字段 + source + ref 信息 + needs_review
  ref.wav             参考音频（从旧目录复制，不改编码）
data/books/<bookId>/
  cover.png           封面（若旧系统有）
  legacy/             旧系统原始件：chapters.json、roles_*.json
  migration.json      迁移与对照报告（分章差异、角色标注一致率）
snapshots/<bookId>-<ts>.zip   JSON 快照（可用 aiab snapshot import 还原）
```

### `voice.json`（示例）

```json
{
  "id": "v001", "name": "暗夜玫瑰", "gender": "女", "age_group": "中年",
  "personality": ["魅惑/诱惑", "慵懒/随性"], "genres": ["都市", "言情"],
  "mood": ["魅惑/诱惑", "浪漫"], "speech_rate": "慢",
  "voice_quality": ["磁性", "低沉"], "language_style": ["普通话"],
  "usage_type": ["角色对话"], "description": "低沉磁性成熟女声…",
  "ref_text": "弟弟，姐姐可不是什么好人…", "mood_hint": "危险·诱惑",
  "prompt": "低沉磁性的成熟女声…", "scenes": "年下恋女主、酒吧老板娘",
  "needs_review": false,
  "source": {"dir": "暗夜玫瑰", "tags_file": "标签.json", "wav": "参考音频.wav"},
  "ref": {"file": "ref.wav", "bytes": 817998, "sample_rate": 44100, "channels": 1, "duration": 4.63}
}
```

### CLI

```powershell
uv run aiab migrate voices --source "D:\workspace\datavrapCastV1.1.0\tts\test\参考音频和文字"
uv run aiab migrate voices --dry-run                     # 只看会写什么
uv run aiab migrate book "D:\...\original.txt" --title "地球最后一个修仙者" `
    --legacy "D:\...\17c3a1cb805940deb0c1ea7d8e8887bd" --cover "D:\...\cover.png"
uv run aiab snapshot export <bookId> --out snapshots/book.zip
uv run aiab snapshot import snapshots/book.zip [--force]
```

---

## Task 1: 音色迁移（96 → data/voices）

**Files:** Create `src/audiobook/migrate/__init__.py`、`migrate/voices.py`；Test `tests/test_migrate_voices.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class VoiceMigrationReport:
    source: Path; target_root: Path; migrated: tuple[str, ...]  # voice ids
    skipped: tuple[str, ...]; needs_review: tuple[str, ...]
    total_bytes: int; dry_run: bool
def build_voice_record(directory: Path, voice_id: str) -> dict
def migrate_voices(settings, source, *, dry_run=False, force=False) -> VoiceMigrationReport
```

- [ ] **Step 1: 测试**（临时造一个 3 音色的假源目录：完整标签、缺标签、只有 wav）

```python
def test_build_voice_record_maps_camel_case_tags(tmp_path):
    src = tmp_path / "暗夜玫瑰"; src.mkdir()
    (src / "标签.json").write_text(json.dumps({
        "name": "暗夜玫瑰", "gender": "女", "ageGroup": "中年",
        "personality": ["魅惑/诱惑"], "genres": ["都市"], "mood": ["浪漫"],
        "speechRate": "慢", "voiceQuality": ["磁性"], "languageStyle": ["普通话"],
        "usageType": ["角色对话"], "description": "低沉磁性成熟女声",
    }, ensure_ascii=False), encoding="utf-8")
    (src / "试听文本.txt").write_text("弟弟，姐姐可不是什么好人。", encoding="utf-8")
    (src / "情绪基调.txt").write_text("危险·诱惑", encoding="utf-8")
    (src / "提示词.txt").write_text("低沉磁性的成熟女声", encoding="utf-8")
    (src / "应用场景.txt").write_text("年下恋女主", encoding="utf-8")
    write_tone(src / "参考音频.wav", seconds=0.3, rate=24000, freq=220)

    record = build_voice_record(src, "v001")
    assert (record["id"], record["name"], record["gender"], record["age_group"]) == ("v001", "暗夜玫瑰", "女", "中年")
    assert record["speech_rate"] == "慢" and record["voice_quality"] == ["磁性"]
    assert record["ref_text"].startswith("弟弟") and record["mood_hint"] == "危险·诱惑"
    assert record["needs_review"] is False
    assert record["ref"]["sample_rate"] == 24000 and record["ref"]["file"] == "ref.wav"


def test_build_voice_record_flags_missing_tags(tmp_path):
    src = tmp_path / "霸道总裁"; src.mkdir()
    (src / "提示词.txt").write_text("低沉磁性的成熟男声", encoding="utf-8")
    write_tone(src / "参考音频.wav", seconds=0.2, rate=24000, freq=220)
    record = build_voice_record(src, "v002")
    assert record["needs_review"] is True
    assert record["gender"] == "男"          # 从提示词里的"男声"推断
    assert record["source"]["tags_file"] is None


def test_migrate_voices_is_deterministic_and_idempotent(settings, tmp_path):
    source = _fake_library(tmp_path)          # 3 个音色
    first = migrate_voices(settings, source)
    assert first.migrated == ("v001", "v002", "v003")
    assert [p.parent.name for p in sorted(settings.voices_dir.glob("*/voice.json"))] == ["v001", "v002", "v003"]
    assert store.voice_ref_path(settings, "v001").exists()
    again = migrate_voices(settings, source)
    assert again.migrated == first.migrated
    assert (settings.voices_dir / "v001" / "ref.wav").read_bytes() == (source / "暗夜玫瑰" / "参考音频.wav").read_bytes()


def test_migrate_voices_dry_run_writes_nothing(settings, tmp_path):
    report = migrate_voices(settings, _fake_library(tmp_path), dry_run=True)
    assert report.dry_run is True and report.migrated
    assert not settings.voices_dir.exists()


def test_migrate_voices_missing_source_raises(settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate_voices(settings, tmp_path / "nope")
```

- [ ] **Step 2: 运行确认失败** → `uv run pytest tests/test_migrate_voices.py -q`
- [ ] **Step 3: 实现** `migrate/voices.py`（要点）

```python
TAG_KEYS = {"name", "gender", "ageGroup", "personality", "genres", "mood", "speechRate",
            "voiceQuality", "languageStyle", "usageType", "description"}
SNAKE = {"ageGroup": "age_group", "speechRate": "speech_rate", "voiceQuality": "voice_quality",
         "languageStyle": "language_style", "usageType": "usage_type"}
TEXT_FILES = {"ref_text": "试听文本.txt", "mood_hint": "情绪基调.txt", "prompt": "提示词.txt", "scenes": "应用场景.txt"}

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""

def _guess_gender(prompt: str, name: str) -> str:
    for token in ("男声", "男生", "男性"):
        if token in prompt or token in name: return "男"
    for token in ("女声", "女生", "女性"):
        if token in prompt or token in name: return "女"
    return "未知"

def build_voice_record(directory: Path, voice_id: str) -> dict:
    tags_path = directory / "标签.json"
    tags = store.read_json(tags_path, default={}) or {}
    record = {"id": voice_id, "name": str(tags.get("name") or directory.name)}
    for key, value in tags.items():
        if key in ("name",) or key not in TAG_KEYS:
            continue
        record[SNAKE.get(key, key)] = value
    prompt = _read_text(directory / "提示词.txt")
    record.setdefault("name", directory.name)
    record.setdefault("gender", _guess_gender(prompt, record["name"]))
    record.setdefault("age_group", "未知")
    for field, filename in TEXT_FILES.items():
        text = _read_text(directory / filename)
        if text:
            record[field] = text
    record["needs_review"] = not tags_path.exists()
    wav = _reference_wav(directory)
    info = probe_wav(wav)
    record["source"] = {"dir": directory.name, "tags_file": tags_path.name if tags_path.exists() else None, "wav": wav.name}
    record["ref"] = {"file": "ref.wav", "bytes": wav.stat().st_size, "sample_rate": info.sample_rate,
                     "channels": info.channels, "duration": round(info.duration, 3)}
    return record
```

`_reference_wav(dir)` 优先 `参考音频.wav`，回退 `ref.wav`/`*.wav`（按名排序第一个）；`migrate_voices` 干：
1. 校验源目录存在，否则 `FileNotFoundError`；
2. 收集所有含 wav 的子目录并按名排序 → `v001…`；
3. 每个音色：写 `voice.json`（`store.atomic_replace_json`）、把 wav 复制成 `ref.wav`（先写 `.tmp` 再 `os.replace`，用 `store.atomic_write_bytes`）；
4. `dry_run` 只统计不落盘；返回报告（含 `needs_review` 列表与总字节数）。

- [ ] **Step 4: 通过 + 提交**

```powershell
git add src/audiobook/migrate tests/test_migrate_voices.py
git commit -m "feat: 音色库迁移（96 音色 → data/voices）" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

## Task 2: 真实音色迁移 + 选角生效验证

**Files:** `docs/migrate.md`（新建）；无新代码（除非发现缺陷）

- [ ] **Step 1: 真跑迁移**

```powershell
uv run aiab migrate voices --source "D:\workspace\datavrapCastV1.1.0\tts\test\参考音频和文字"
```
Expected: `96 个音色 → data/voices/`，`needs_review: 1`（霸道总裁），总字节数 ≈ 76MB（只复制 wav）

- [ ] **Step 2: 用 API/选角验证**

```powershell
uv run aiab run <bookId>          # 重新入队 casting（见下：删掉 casting.json 后 run 会重排）
uv run aiab worker --max-jobs 1
```
Expected: `casting.json` 里 `voice_library_size = 96`，角色拿到不同音色（不再全是 `default`），`issues.jsonl` 不再有 `voice_library_empty`

- [ ] **Step 3: 提交文档**

```powershell
git add docs/migrate.md
git commit -m "docs: 音色库迁移说明与实跑记录" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

## Task 3: 书籍迁移与旧系统对照

**Files:** Create `migrate/books.py`、`migrate/compare.py`；Test `tests/test_migrate_books.py`、`tests/test_migrate_compare.py`

**Interfaces:**

```python
def migrate_book(settings, conn, txt: Path, *, title: str, legacy_dir: Path | None = None,
                 cover: Path | None = None, book_id: str | None = None) -> dict   # 返回 migration.json 内容
# compare.py
def compare_chapters(old_chapters: list[dict], new_chapters: list[dict]) -> dict
def compare_roles(old_roles: list[list[dict]], new_lines: dict[int, list[dict]]) -> dict
```

- [ ] **Step 1: 测试**

```python
def test_compare_chapters_reports_title_and_char_diffs():
    old = [{"index": 0, "title": "前言", "content": "x" * 10}, {"index": 1, "title": "卷一", "content": "y" * 100}]
    new = [{"index": 0, "title": "卷一", "content": "y" * 90}]
    result = compare_chapters(old, new)
    assert result["old_count"] == 2 and result["new_count"] == 1
    assert result["rows"][0]["title_match"] is False
    assert result["rows"][0]["chars_delta"] == -10


def test_compare_roles_matches_sentences_and_counts_agreement():
    old = [[{"text": "秦风，你为什么要杀我", "role": "张卫东"}, {"text": "燕京的郊外。", "role": "旁白"}]]
    new = {0: [{"text": "秦风，你为什么要杀我。", "speaker_name": "张卫东"},
               {"text": "燕京的郊外。", "speaker_name": "旁白"},
               {"text": "别的句子。", "speaker_name": "旁白"}]}
    result = compare_roles(old, new)
    assert (result["old_lines"], result["matched"], result["agree"]) == (2, 2, 2)
    assert result["agreement_rate"] == 1.0
    assert result["mismatches"] == []


def test_migrate_book_copies_cover_legacy_and_writes_report(settings, conn, tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("第一章 起风\n\n第一句。第二句。\n", encoding="utf-8")
    legacy = tmp_path / "legacy"; legacy.mkdir()
    (legacy / "chapters.json").write_text(json.dumps([{"index": 0, "title": "第一章 起风", "content": "x"}]), encoding="utf-8")
    (legacy / "roles_0.json").write_text(json.dumps([{"text": "第一句。", "role": "旁白"}]), encoding="utf-8")
    cover = tmp_path / "cover.png"; cover.write_bytes(b"\x89PNG\r\n\x1a\n")

    report = migrate_book(settings, conn, txt, title="样本书", legacy_dir=legacy, cover=cover, book_id="mig1")
    book_dir = store.book_dir(settings, "mig1")
    assert (book_dir / "cover.png").read_bytes() == cover.read_bytes()
    assert (book_dir / "legacy" / "roles_0.json").exists()
    assert report["book_id"] == "mig1"
    assert report["chapters"]["new_count"] == 1
    assert report["source"]["sha256"]
    assert store.read_json(book_dir / "migration.json")["book_id"] == "mig1"
    assert store.read_json(book_dir / "book.json")["cover"] == "cover.png"
```

- [ ] **Step 2: 运行确认失败** → `uv run pytest tests/test_migrate_books.py tests/test_migrate_compare.py -q`
- [ ] **Step 3: 实现**
  - `compare_chapters`：按 index 对齐，逐章输出 `{index, old_title, new_title, title_match, old_chars, new_chars, chars_delta}`；
  - `compare_roles`：把旧句子按"去标点空白"归一化做键，与新 `lines` 建索引比对；统计 `old_lines/matched/agree/mismatch_samples(最多 20 条)`；
  - `migrate_book`：调用既有 `importer.import_book`（可传 book_id）→ 复制 cover → 复制 legacy 目录 → 读旧 chapters.json 与新 chapters.json 跑 `compare_chapters` → 写 `migration.json`（含源文件 sha256、章节对照、旧 roles 文件清单；若旧系统已有 roles 就一并算 `roles` 对照，但新系统此刻还没跑分析，所以记 `roles_compare: null` 并留待 `aiab compare` 补）。
  - 补一个 `aiab compare <bookId> --legacy <dir>` 在分析完成后重算角色对照并写回 `migration.json`。

- [ ] **Step 4: 通过 + 提交**

```powershell
git add src/audiobook/migrate tests/test_migrate_books.py tests/test_migrate_compare.py
git commit -m "feat: 书籍迁移与旧系统对照" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

## Task 4: CLI（migrate / compare / snapshot）

**Files:** Create `migrate/snapshot.py`；Modify `cli.py`；Test `tests/test_cli_migrate.py`、`tests/test_snapshot.py`

**Interfaces:**

```python
# snapshot.py
def export_snapshot(settings, book_id: str, out: Path) -> dict     # 返回 manifest
def import_snapshot(settings, conn, path: Path, *, force=False, book_id: str | None = None) -> dict
```

```
aiab migrate voices --source DIR [--dry-run]
aiab migrate book TXT --title T [--legacy DIR] [--cover PNG] [--book-id ID]
aiab compare BOOK_ID --legacy DIR
aiab snapshot export BOOK_ID [--out FILE]
aiab snapshot import FILE [--force] [--book-id ID]
```

- [ ] **Step 1: 测试**

```python
def test_snapshot_round_trip(settings, conn, narrator_lines, tmp_path):
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    store.write_jsonl_atomic(store.lines_path(settings, "b1", 0), narrator_lines(0, "第一句。"))
    out = tmp_path / "snap.zip"
    manifest = export_snapshot(settings, "b1", out)
    assert manifest["book_id"] == "b1" and manifest["files"] >= 2 and out.exists()

    shutil.rmtree(store.book_dir(settings, "b1"))
    import_snapshot(settings, conn, out)
    assert store.read_json(store.book_dir(settings, "b1") / "book.json")["title"] == "T"
    assert len(store.read_jsonl(store.lines_path(settings, "b1", 0))) == 2
    with pytest.raises(FileExistsError):
        import_snapshot(settings, conn, out)
    assert import_snapshot(settings, conn, out, force=True)["restored"] >= 2


def test_snapshot_does_not_include_audio(settings, conn, tmp_path):
    store.atomic_write_bytes(store.chapter_wav_path(settings, "b1", 0), b"RIFF")
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"id": "b1", "title": "T"})
    manifest = export_snapshot(settings, "b1", tmp_path / "s.zip")
    assert all(not name.endswith((".wav", ".mp3", ".mkv")) for name in manifest["names"])


def test_cli_migrate_voices_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AB_DATA_DIR", str(tmp_path / "data"))
    source = _fake_library(tmp_path)
    assert main(["migrate", "voices", "--source", str(source), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "3" in out
    assert not (tmp_path / "data" / "voices").exists()
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**：`snapshot.py` 用 `zipfile.ZipFile`（`ZIP_DEFLATED`）打包书目录下所有 `*.json`/`*.jsonl`（含 `analysis/**`、`voices/casting.json`），清单写 `snapshot.json`（book id/title/时间/文件数与每个文件的 sha256）；导入时校验清单、把文件解到 `data/books/<id>/`（`--book-id` 可改 id 并同步改写 book.json/DB 行），已存在且无 `--force` 抛 `FileExistsError`；CLI 各自打印摘要，错误返回码 1/2。
- [ ] **Step 4: 通过 + 提交**

```powershell
git add src/audiobook/migrate/snapshot.py src/audiobook/cli.py tests/test_cli_migrate.py tests/test_snapshot.py
git commit -m "feat: migrate/compare/snapshot 子命令" -m "Co-authored-by: Codex <codex@openai.com>"
```

---

## Task 5: 样本书真跑（迁移 → 分析 → 选角 → 合成 → 导出）

- [ ] **Step 1: 迁移**

```powershell
uv run aiab migrate book "D:\workspace\datavrapCastV1.1.0\data\audiobook\17c3a1cb805940deb0c1ea7d8e8887bd\original.txt" `
  --title "地球最后一个修仙者" `
  --legacy "D:\workspace\datavrapCastV1.1.0\data\audiobook\17c3a1cb805940deb0c1ea7d8e8887bd" `
  --cover  "D:\workspace\datavrapCastV1.1.0\data\audiobook\17c3a1cb805940deb0c1ea7d8e8887bd\cover.png"
```

- [ ] **Step 2: 分析 + 选角**：`uv run aiab run <bookId>; uv run aiab worker`（LLM 用本机网关，串行观察 `jobs`）
- [ ] **Step 3: 与旧系统对照**：`uv run aiab compare <bookId> --legacy "...\17c3a1..."` → 记录章节差异与角色标注一致率
- [ ] **Step 4: 合成 + 导出**：起 fake TTS 服务（`uv run --project tts aiab-tts serve --backend fake --port 8020`），`.env` 里 `AB_ENGINE=http`、`AB_TTS_ENDPOINTS=["http://127.0.0.1:8020"]`，跑 `aiab run` + `worker`，最后 `aiab export`
- [ ] **Step 5: 快照**：`uv run aiab snapshot export <bookId>` 并验证可导入

---

## M6 验收

| 项 | 怎么做 | 现在能不能做 |
|---|---|---|
| C1 单元测试 | `uv run pytest` | ✅ 本地 |
| C2 96 音色迁移 | `aiab migrate voices` + `GET /api/voices` 返回 96 条、试听返回 200 | ✅ 本地 |
| C3 选角生效 | 重跑 casting：`voice_library_size=96`，角色不再全是 `default` | ✅ 本地 |
| C4 样本书迁移 | `aiab migrate book ...` 产出 `migration.json`（含分章对照） | ✅ 本地 |
| C5 角色标注对照 | `aiab compare` 输出一致率与不匹配样本 | ✅ 本地（LLM 已可用） |
| C6 全新链路 | 迁移后的书跑完 分析 → 合成 → 导出 | ✅ 本地（fake TTS） |
| C7 快照往返 | `snapshot export/import` 还原 JSON 状态 | ✅ 本地 |
| C8 真实音质试听 | GPU 机器上用真实音色跑一章并试听 | ⛔ 需要 GPU 与用户设备 |

## 自检（写计划时已核对）

1. **Spec 覆盖**：§10.1（Task 1/2）、§10.2（Task 3/5）、§10.3（Task 4 快照 + CLI）、§12.6 迁移测试（Task 1/3/4 的测试）、§5.4 选角读 `voice.json`（Task 2 验证）。
2. **不做的事**：不用旧 `roles_*.json` 当分析输入；不迁移 mp3（只保留 wav）；不做全库快照。
3. **类型一致性**：`voice.json` 字段与 `analysis/casting.load_voice_library` 的读取键一一对应（snake_case 优先、camelCase 兼容）；`store.voice_ref_path` 是 TTS 侧唯一引用路径；快照清单字段在导出/导入两侧同名。
4. **已知取舍**：音色 id 用排序序号（源库冻结、可复现）；缺标签的音色标 `needs_review` 而不是伪造字段；书籍迁移创建新 bookId（老 id 的既有产物不动）。

---

## M6 验收记录（2026-09-24 实跑）

环境：本机 Windows + PowerShell，Python 3.13；旧系统只读引用 `D:\workspace\datavrapCastV1.1.0`。

| 项 | 结果 |
|---|---|
| C1 单元测试 | `uv run pytest` → **296 passed**（M6 新增 24 个用例） |
| C2 96 音色迁移 | `aiab migrate voices` → 96 个、参考音频 61.1 MB、待补标签 1（v094 霸道总裁）、缺参考音频 0；`GET /api/voices` 返回 96 条，`/api/voices/v001/sample` → 200 / audio/wav / 433 KB |
| C3 选角生效 | 重跑 casting：`voice_library_size=96`，14 个角色全部分到真实音色（王胖子→v058 热血兄弟、许老爷子→v041 沉稳老者、黄老板→v023 市井小贩…），不再有 `default` 与 `voice_library_empty` |
| C4 样本书迁移 | `aiab migrate book …` → 新 bookId `2a48641cedee4e7e8bfaeece8012dfbb`；**旧 10 章 → 新 9 章，标题命中 9，字符差 -3218**，仅旧系统有"前言"（推广页，被清洗规则丢弃）；封面与 10 个 `roles_*.json` 已留档 |
| C5 角色标注对照 | `aiab compare` → 旧标注 846 句 / 匹配 812 / 一致 673 → **一致率 82.9%**；139 处不一致**全部**是"旧旁白→新角色"，反向 0 句 |
| C6 全新链路 | 迁移后的书：38 个分析任务全绿 → 合成 818 句用 **14 个音色**（旁白 v014 ×665、张卫东 v001 ×71、王胖子 v058 ×28…）→ 9 章渲染 + 整本 `book.wav/srt/mkv`（1547.72s / 818 条字幕 / 15 个产物）；1 条 `scene_hint_not_found` 降级（该章场景边界定位失败 → 均匀切分，已进 `issues.jsonl`） |
| C7 快照往返 | 863 个 JSON 导出 → 以新 id 导入 → `book.json`/`casting.json` 的 id 改写正确、章节与句子数一致；测试副本验证后已删除 |
| C8 真实音质 | ⛔ 需要 GPU 机器按 `docs/tts-deploy.md` 跑真机 |

实跑修掉/发现的问题：

1. **迁移拿不到分章结果**：`import_book` 只入队 `chapter_split`，迁移当场读 `chapters.json` 会拿到空 → 把分章逻辑抽成 `handlers.split.split_book()`，迁移同步执行并撤掉刚入队的任务。
2. **对照错位**：旧章节标题带"（新书求收藏）"后缀时，标题匹配失败退化成按位置配对，整张对照表右移一格 → 增加"去括号后缀"的模糊标题匹配（`title_loose`），命中数从 5 升到 9。
3. **差异无法解释**：只统计"一致率"看不出好坏 → 增加 `legacy_narrator_reassigned` / `new_narrator_fallback` 两个分类计数，才看清 139 处差异全是新系统更准。

遗留：

1. `霸道总裁`（v094）标签待补，字段补齐后把 `needs_review` 改成 `false`。
2. 参考音频未统一格式（旧库采样率/声道不一），需要的话用 ffmpeg 批量转换。
3. 真机音质与显存表现要按 `docs/tts-deploy.md` 在 GPU 机器上复测。
4. M1 时代那本 `0e686403…` 仍保留（已用真实音色重跑过一轮），它是早期验收的存档，不是迁移产物。
