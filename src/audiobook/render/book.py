import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import store
from . import srt as srt_mod
from .container import write_media
from .ffmpeg import probe_wav
from .mix import clip_from_wav, concat_clips, ensure_uniform
from .naming import book_title, chapter_label

RANGE = re.compile(r"^\s*(\d+)\s*(?:\.\.|-)\s*(\d+)\s*$")


@dataclass(frozen=True)
class ExportReport:
    mode: str
    container: str
    out_dir: Path
    chapters: tuple[int, ...]
    missing: tuple[int, ...]
    outputs: tuple[Path, ...]
    total_seconds: float
    cues: int
    warnings: tuple[str, ...] = ()
    dry_run: bool = False


def parse_chapter_filter(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    picked: set[int] = set()
    for token in re.split(r"[,;\s]+", str(spec).strip()):
        if not token:
            continue
        found = RANGE.match(token)
        if found:
            low, high = sorted((int(found.group(1)), int(found.group(2))))
            picked.update(range(low, high + 1))
        elif token.isdigit():
            picked.add(int(token))
        else:
            raise ValueError(f"无法解析章节筛选：{token}（示例 1,2,30-38）")
    return picked or None


def _available_indexes(settings, book_id: str) -> list[int]:
    chapters = (store.read_json(store.chapters_path(settings, book_id), default={}) or {}).get("chapters") or []
    if chapters:
        return [int(chapter["index"]) for chapter in chapters]
    indexes: list[int] = []
    for path in sorted(store.output_dir(settings, book_id).glob("chapter_*.wav")):
        digits = path.stem.rsplit("_", 1)[-1]
        if digits.isdigit():
            indexes.append(int(digits))
    return indexes


def _hms(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _write_playlist(out_dir: Path, pairs, fmt: str) -> Path:
    lines = ["#EXTM3U"]
    for index, wav, _srt_path in sorted(pairs):
        seconds = int(round(probe_wav(wav).duration))
        lines.append(f"#EXTINF:{seconds},第{index}章")
        lines.append(f"chapter_{store.chapter_tag(index)}.{fmt}")
    path = out_dir / "playlist.m3u"
    store.atomic_write_text(path, "\r\n".join(lines) + "\r\n")
    return path


def _write_marks(out_dir: Path, marks) -> Path:
    body = [f"{label}  [{_hms(start)} - {_hms(end)}]" for label, start, end in marks]
    path = out_dir / "book_章节.txt"
    store.atomic_write_text(path, "\n".join(body) + "\n")
    return path


def _write_ffmetadata(path: Path, title: str, marks) -> None:
    lines = [";FFMETADATA1", f"title={title}"]
    for label, start, end in marks:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(start * 1000))}",
            f"END={int(round(end * 1000))}",
            f"title={label}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_report(out_dir: Path, book_id: str, marks, cues: int, outputs: list[Path]) -> Path:
    lines = [
        f"生成时间: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}",
        f"书籍: {book_id}",
        f"章节数: {len(marks)}",
        f"字幕: {cues} 条",
        "",
        "章节明细:",
    ]
    lines += [f"  {label}  [{_hms(start)} - {_hms(end)}]" for label, start, end in marks]
    lines += ["", "输出文件:"]
    lines += [f"  {path.name}" for path in outputs]
    path = out_dir / "merge-report.txt"
    store.atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def export_book(
    settings,
    book_id: str,
    *,
    mode: str = "all",
    container: str | None = None,
    chapters: set[int] | None = None,
    out_dir=None,
    force: bool = False,
    dry_run: bool = False,
    containers: bool | None = None,
    on_progress=None,
) -> ExportReport:
    if mode not in {"chapter", "book", "all"}:
        raise ValueError(f"未知导出模式：{mode}")
    fmt = (container or settings.export_container or "mkv").lower()
    if fmt not in {"mkv", "mp4"}:
        raise ValueError(f"不支持的容器：{fmt}")
    want_containers = settings.export_mkv if containers is None else bool(containers)
    target_dir = store.export_target_dir(settings, book_id, out_dir)
    # 指定了 --chapters 就以它为准（用户点名要的章节缺产物就是 missing）；
    # 否则以 chapters.json 为准（清单里有但还没出成品的章节算 missing）。
    expected = sorted(chapters) if chapters else _available_indexes(settings, book_id)
    selected = expected
    pairs: list[tuple[int, Path, Path]] = []
    missing: list[int] = []
    for index in selected:
        wav = store.chapter_wav_path(settings, book_id, index)
        srt_path = store.chapter_srt_path(settings, book_id, index)
        if wav.exists() and srt_path.exists():
            pairs.append((index, wav, srt_path))
        else:
            missing.append(index)
    warnings: list[str] = [f"第 {index} 章缺少 wav/srt，已跳过" for index in missing]
    outputs: list[Path] = []

    def report(done: int, total: int, message: str) -> None:
        if on_progress is not None:
            on_progress(done, total, message)

    if not pairs:
        warnings.append("没有任何已完成章节（需要 chapter_*.wav 与 .srt 同时存在）")
        return ExportReport(mode, fmt, target_dir, (), tuple(missing), (), 0.0, 0, tuple(warnings), dry_run)

    if dry_run:
        planned = [target_dir / f"chapter_{store.chapter_tag(index)}.{fmt}" for index, _, _ in pairs]
        if mode in {"book", "all"}:
            planned += [target_dir / "book.wav", target_dir / "book.srt"]
            if want_containers:
                planned.append(target_dir / f"book.{fmt}")
        return ExportReport(
            mode,
            fmt,
            target_dir,
            tuple(index for index, _, _ in pairs),
            tuple(missing),
            tuple(planned),
            0.0,
            0,
            tuple(warnings),
            True,
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    if mode in {"chapter", "all"} and want_containers:
        for index, wav, srt_path in pairs:
            dst = target_dir / f"chapter_{store.chapter_tag(index)}.{fmt}"
            newest_input = max(wav.stat().st_mtime, srt_path.stat().st_mtime)
            if dst.exists() and not force and dst.stat().st_mtime >= newest_input:
                outputs.append(dst)
                continue
            report(0, 1, f"封装第 {index} 章")
            write_media(
                settings,
                audio=wav,
                subtitles=srt_path,
                dst=dst,
                container=fmt,
                title=chapter_label(settings, book_id, index),
            )
            outputs.append(dst)
        outputs.append(_write_playlist(target_dir, pairs, fmt))

    total_seconds = 0.0
    cue_count = 0
    if mode in {"book", "all"}:
        ordered = sorted(pairs, key=lambda item: item[0])
        work_dir = target_dir / "_book_work"
        clips = [clip_from_wav(f"chapter_{store.chapter_tag(index)}", wav) for index, wav, _ in ordered]
        report(0, 1, "统一整本采样率")
        uniform, _target_rate = ensure_uniform(settings, clips, work_dir)
        book_wav = target_dir / "book.wav"
        report(0, 1, "拼接整本音频")
        concat_clips(uniform, book_wav)
        cues: list[srt_mod.Cue] = []
        marks: list[tuple[str, float, float]] = []
        offset = 0.0
        for (index, _wav, srt_path), clip in zip(ordered, uniform):
            chapter_cues = srt_mod.parse_srt(srt_path)
            cues.extend(srt_mod.shift_cues(chapter_cues, offset, limit=offset + clip.duration))
            marks.append((chapter_label(settings, book_id, index), offset, offset + clip.duration))
            offset += clip.duration
        book_srt = target_dir / "book.srt"
        srt_mod.write_srt(cues, book_srt)
        actual = probe_wav(book_wav).duration
        if abs(actual - offset) > 0.5:
            warnings.append(f"整本音频 {actual:.2f}s 与章节时长之和 {offset:.2f}s 偏差过大")
        total_seconds, cue_count = actual, len(cues)
        outputs += [book_wav, book_srt, _write_marks(target_dir, marks)]
        if want_containers:
            ffmeta = target_dir / "book_chapters.ffmeta"
            _write_ffmetadata(ffmeta, book_title(settings, book_id), marks)
            report(0, 1, "封装整本容器")
            book_media = target_dir / f"book.{fmt}"
            write_media(
                settings,
                audio=book_wav,
                subtitles=book_srt,
                dst=book_media,
                container=fmt,
                title=f"{book_title(settings, book_id)} 有声书",
                ffmetadata=ffmeta,
            )
            ffmeta.unlink(missing_ok=True)
            outputs.append(book_media)
        outputs.append(_write_report(target_dir, book_id, marks, cue_count, outputs))
        shutil.rmtree(work_dir, ignore_errors=True)
    return ExportReport(
        mode,
        fmt,
        target_dir,
        tuple(index for index, _, _ in pairs),
        tuple(missing),
        tuple(outputs),
        total_seconds,
        cue_count,
        tuple(warnings),
    )
