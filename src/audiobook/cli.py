import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

from .config import get_settings
from .db import connect, init_db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aiab")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务（只做快请求）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8300)

    p_worker = sub.add_parser("worker", help="启动 worker（长任务）")
    p_worker.add_argument("--once", action="store_true", help="只领一个任务后退出（用于测试）")
    p_worker.add_argument("--max-jobs", type=int, default=None, help="最多执行 N 个任务后退出（用于验收）")
    p_worker.add_argument("--worker-id", default=None)

    p_import = sub.add_parser("import", help="导入 txt / epub 并入队分章")
    p_import.add_argument("txt", help="书稿路径（.txt 或 .epub）")
    p_import.add_argument("--title", required=True)
    p_import.add_argument("--book-id", default=None)

    p_run = sub.add_parser("run", help="按文件断点为一本书入队下一步任务")
    p_run.add_argument("book_id")

    p_export = sub.add_parser("export", help="导出可播放的容器与整本合本")
    p_export.add_argument("book_id")
    p_export.add_argument("--mode", choices=["chapter", "book", "all"], default="all")
    p_export.add_argument("--container", choices=["mkv", "mp4"], default=None)
    p_export.add_argument("--chapters", default=None, help="例如 1,2,30-38")
    p_export.add_argument("--out-dir", default=None)
    p_export.add_argument("--no-container", action="store_true", help="只出 wav/srt，不封装 mkv/mp4")
    p_export.add_argument("--force", action="store_true")
    p_export.add_argument("--dry-run", action="store_true")

    sub.add_parser("llm-check", help="验证 LLM 端点连通性并做一次 JSON 往返")

    p_tts = sub.add_parser("tts", help="一键启动/停止本机 TTS 推理服务（tts/ 子项目）")
    tts_sub = p_tts.add_subparsers(dest="tts_cmd", required=True)
    p_tts_start = tts_sub.add_parser("start")
    p_tts_start.add_argument("--backend", choices=["indextts"], default=None)
    p_tts_start.add_argument("--port", type=int, default=None)
    p_tts_start.add_argument("--model-source", choices=["modelscope", "huggingface", "local"], default=None)
    p_tts_start.add_argument("--model-dir", default=None)
    p_tts_start.add_argument("--wait", type=float, default=0.0, help="等健康检查通过的秒数（0=不等）")
    tts_sub.add_parser("stop")
    tts_sub.add_parser("status")
    p_tts_logs = tts_sub.add_parser("logs")
    p_tts_logs.add_argument("--lines", type=int, default=40)

    p_migrate = sub.add_parser("migrate", help="从旧系统迁移音色库或书籍")
    migrate_sub = p_migrate.add_subparsers(dest="what", required=True)
    p_migrate_voices = migrate_sub.add_parser("voices", help="迁移内置音色库到 data/voices")
    p_migrate_voices.add_argument("--source", required=True, help="旧音色库目录（含各音色子目录）")
    p_migrate_voices.add_argument("--dry-run", action="store_true")
    p_migrate_voices.add_argument("--force", action="store_true", help="连参考音频也重新复制")
    p_migrate_book = migrate_sub.add_parser("book", help="迁移一本书（清洗分章 + 封面 + 旧件留档）")
    p_migrate_book.add_argument("txt")
    p_migrate_book.add_argument("--title", required=True)
    p_migrate_book.add_argument("--legacy", default=None, help="旧系统的任务目录（含 chapters.json / roles_*.json）")
    p_migrate_book.add_argument("--cover", default=None)
    p_migrate_book.add_argument("--book-id", default=None)

    p_compare = sub.add_parser("compare", help="用旧 roles_*.json 复核新系统的说话人标注")
    p_compare.add_argument("book_id")
    p_compare.add_argument("--legacy", required=True)

    p_snapshot = sub.add_parser("snapshot", help="导出/导入单本书的 JSON 快照")
    snapshot_sub = p_snapshot.add_subparsers(dest="snapshot_cmd", required=True)
    p_snapshot_export = snapshot_sub.add_parser("export")
    p_snapshot_export.add_argument("book_id")
    p_snapshot_export.add_argument("--out", default=None)
    p_snapshot_import = snapshot_sub.add_parser("import")
    p_snapshot_import.add_argument("path")
    p_snapshot_import.add_argument("--force", action="store_true")
    p_snapshot_import.add_argument("--book-id", default=None)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "llm-check":
        from .analysis.models import MergeOutput
        from .llm.base import LLMError
        from .llm.limiter import AdaptiveLimiter
        from .llm.openai_compat import build_client
        from .llm.runner import LlmJsonError, LlmJsonRunner

        try:
            runner = LlmJsonRunner(
                build_client(settings), AdaptiveLimiter(max_concurrency=settings.llm_concurrency), settings
            )
            result = runner.run(
                system="你是 JSON 生成器，只输出 JSON。",
                user='只输出 {"characters": []}',
                model_cls=MergeOutput,
                pass_name="check",
                book_id="",
            )
        except (LLMError, LlmJsonError) as exc:
            print(f"LLM 不可用：{exc}")
            return 2
        print(f"LLM 可用：{settings.llm_base_url} / {settings.llm_model} → {result.model_dump()}")
        return 0

    if args.cmd == "tts":
        import time as _time

        from .config import save_overlay
        from .tts_service import LocalTtsService

        service = LocalTtsService(settings)
        if args.tts_cmd == "status":
            info = service.status()
            state = "运行中" if info["running"] else "未运行"
            health = "健康" if info["healthy"] else ("启动中" if info["starting"] else "无响应")
            print(f"TTS {state}：{info['url']}（backend={info['backend']} pid={info['pid']} {health}）")
            print(f"日志：{info['log_path']}")
            return 0
        if args.tts_cmd == "logs":
            payload = service.logs(limit=args.lines)
            for line in payload["lines"]:
                print(line)
            return 0
        if args.tts_cmd == "stop":
            info = service.stop()
            print(f"已停止 TTS：{info['url']}")
            return 0
        try:
            info = service.start(
                backend=args.backend,
                port=args.port,
                model_source=args.model_source,
                model_dir=args.model_dir,
            )
        except (RuntimeError, OSError) as exc:
            print(f"启动失败：{exc}")
            return 1
        deadline = _time.time() + max(0.0, args.wait)
        while args.wait and not service.status()["healthy"] and _time.time() < deadline:
            _time.sleep(2.0)
        info = service.status()
        print(f"TTS 已启动：{info['url']}（backend={info['backend']} pid={info['pid']}）")
        print(f"健康检查：{'通过' if info['healthy'] else '还没通过（模型加载中或启动失败，看日志）'}")
        overlay = save_overlay(settings, {"engine": "http", "tts_endpoints": [info["url"]]})
        print(f"已把合成引擎切到 http → {info['url']}（{', '.join(sorted(overlay))}）")
        print(f"日志：{info['log_path']}")
        return 0

    if args.cmd == "run":
        from .pipeline import resume_book

        conn = connect(settings.db_path)
        init_db(conn)
        plan = resume_book(settings, conn, args.book_id)
        text = "、".join(f"{kind}#{chapter}" for kind, chapter in plan) or "（无，全部已完成）"
        print(f"入队：{text}")
        return 0

    if args.cmd == "migrate" and args.what == "voices":
        from .migrate.voices import migrate_voices

        try:
            report = migrate_voices(settings, args.source, dry_run=args.dry_run, force=args.force)
        except FileNotFoundError as exc:
            print(f"参数错误：{exc}")
            return 2
        mode = "dry-run：" if report.dry_run else ""
        print(f"{mode}迁移 {len(report.migrated)} 个音色 → {report.target_root}")
        print(f"  参考音频合计 {report.total_bytes / 1024 / 1024:.1f} MB")
        if report.needs_review:
            print(f"  待补标签：{', '.join(report.needs_review)}")
        if report.skipped:
            print(f"  跳过（无 wav）：{', '.join(report.skipped)}")
        if not report.dry_run:
            print(f"  区间：{report.migrated[0]} … {report.migrated[-1]}" if report.migrated else "  （无）")
        return 0

    if args.cmd == "migrate" and args.what == "book":
        from .migrate.books import migrate_book
        from . import store

        conn = connect(settings.db_path)
        init_db(conn)
        txt = Path(args.txt)
        if not txt.exists():
            print(f"参数错误：找不到 {txt}")
            return 2
        report = migrate_book(
            settings,
            conn,
            txt,
            title=args.title,
            legacy_dir=Path(args.legacy) if args.legacy else None,
            cover=Path(args.cover) if args.cover else None,
            book_id=args.book_id,
        )
        chapters = report["chapters"]
        print(f"book_id: {report['book_id']}")
        print(f"章节：旧 {chapters['old_count']} → 新 {chapters['new_count']}（标题命中 {chapters['title_match_count']}）")
        print(f"字符差：{chapters['chars_delta_total']:+d}")
        if chapters["old_only"]:
            print(f"  仅旧系统有：{'、'.join(chapters['old_only'][:6])}")
        if report["legacy"]["roles_files"]:
            print(f"  旧角色文件 {len(report['legacy']['roles_files'])} 个已留档到 legacy/")
        print(f"报告：{store.book_dir(settings, report['book_id']) / 'migration.json'}")
        return 0

    if args.cmd == "compare":
        from .migrate.books import compare_book_roles

        result = compare_book_roles(settings, args.book_id, Path(args.legacy))
        print(
            f"旧标注 {result['old_lines']} 句，匹配上 {result['matched']} 句，"
            f"其中一致 {result['agree']} 句"
        )
        if result["agreement_rate"] is not None:
            print(f"一致率：{result['agreement_rate'] * 100:.1f}%")
        print(
            f"  不一致 {result['mismatch_total']} 句：旧旁白→新角色 {result['legacy_narrator_reassigned']}，"
            f"旧角色→新旁白 {result['new_narrator_fallback']}"
        )
        for row in result["mismatches"][:6]:
            print(f"  不一致：旧={row['legacy']} 新={row['new']} | {str(row['text'])[:40]}")
        return 0

    if args.cmd == "snapshot":
        from .migrate.snapshot import default_snapshot_path, export_snapshot, import_snapshot

        conn = connect(settings.db_path)
        init_db(conn)
        if args.snapshot_cmd == "export":
            out = Path(args.out) if args.out else default_snapshot_path(settings, args.book_id)
            try:
                manifest = export_snapshot(settings, args.book_id, out)
            except FileNotFoundError as exc:
                print(f"参数错误：{exc}")
                return 2
            print(f"已导出 {manifest['files']} 个 JSON 文件 → {out}")
            return 0
        try:
            result = import_snapshot(settings, conn, Path(args.path), force=args.force, book_id=args.book_id)
        except FileExistsError as exc:
            print(f"导入失败：{exc}")
            return 1
        except (FileNotFoundError, ValueError, KeyError) as exc:
            print(f"导入失败：{exc}")
            return 1
        print(f"已还原 {result['restored']} 个文件 → book_id={result['book_id']}（{result['title']}）")
        return 0

    if args.cmd == "export":
        from .render.book import export_book, parse_chapter_filter
        from .render.ffmpeg import FFmpegError

        try:
            chapters = parse_chapter_filter(args.chapters)
        except ValueError as exc:
            print(f"参数错误：{exc}")
            return 2
        try:
            report = export_book(
                settings,
                args.book_id,
                mode=args.mode,
                container=args.container,
                chapters=chapters,
                out_dir=args.out_dir,
                force=args.force,
                dry_run=args.dry_run,
                containers=False if args.no_container else None,
            )
        except (FFmpegError, RuntimeError, ValueError) as exc:
            print(f"导出失败：{exc}")
            return 1
        for warning in report.warnings:
            print(f"警告：{warning}")
        print(f"模式 {report.mode} / 容器 {report.container} / 输出目录 {report.out_dir}")
        print(
            f"章节 {len(report.chapters)} 个，缺失 {len(report.missing)} 个，"
            f"字幕 {report.cues} 条，总时长 {report.total_seconds:.1f}s"
        )
        for path in report.outputs:
            print(f"  {path}")
        if report.dry_run:
            print("（dry-run：未写入任何文件）")
        return 0

    if args.cmd == "import":
        from .importer import import_book

        conn = connect(settings.db_path)
        init_db(conn)
        book_id = import_book(settings, conn, Path(args.txt), title=args.title, book_id=args.book_id)
        print(book_id)
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .api.app import create_app
        from .engines.factory import build_engine

        conn = connect(settings.db_path)
        init_db(conn)
        try:
            probe = build_engine(settings)
            if hasattr(probe, "refresh"):
                probe.refresh(force=True)
                print(f"TTS 状态：{probe.status()}")
            close = getattr(probe, "close", None)
            if callable(close):
                close()
        except Exception as exc:  # noqa: BLE001 - TTS 没起也要能起服务
            print(f"警告：TTS 未就绪（{exc}）。请先启动 TTS 服务再跑合成任务。")
        uvicorn.run(create_app(settings, conn), host=args.host, port=args.port)
        return 0

    if args.cmd == "worker":
        from .engines.factory import build_engine
        from .handlers import (  # noqa: F401
            book_export,
            casting,
            characters,
            lines,
            post,
            split,
            synthesize,
            synthesize_line,
        )
        from .llm.base import LLMError
        from .llm.limiter import AdaptiveLimiter
        from .llm.openai_compat import build_client
        from .llm.runner import LlmJsonRunner
        from .worker import WorkerContext, run_forever, run_once

        conn = connect(settings.db_path)
        init_db(conn)
        llm = None
        try:
            llm = LlmJsonRunner(
                build_client(settings),
                AdaptiveLimiter(max_concurrency=settings.llm_concurrency),
                settings,
            )
        except LLMError as exc:
            print(f"警告：{exc}（分析类任务会失败）")
        ctx = WorkerContext(
            settings=settings,
            conn=conn,
            worker_id=args.worker_id or f"w-{os.getpid()}-{uuid.uuid4().hex[:6]}",
            engine=build_engine(settings),
            llm=llm,
            # 每轮任务前重读 data/settings.json：改并发 / 端点 / 引擎不用重启 worker
            reload_settings=lambda: get_settings(),
            engine_factory=build_engine,
            llm_factory=lambda fresh: LlmJsonRunner(
                build_client(fresh),
                AdaptiveLimiter(max_concurrency=fresh.llm_concurrency),
                fresh,
            ),
        )
        if args.once:
            run_once(ctx)
        else:
            run_forever(ctx, max_jobs=args.max_jobs)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
