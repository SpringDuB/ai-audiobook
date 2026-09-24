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

    p_import = sub.add_parser("import", help="导入 txt 并入队分章")
    p_import.add_argument("txt")
    p_import.add_argument("--title", required=True)
    p_import.add_argument("--book-id", default=None)

    p_run = sub.add_parser("run", help="按文件断点为一本书入队下一步任务")
    p_run.add_argument("book_id")

    sub.add_parser("llm-check", help="验证 LLM 端点连通性并做一次 JSON 往返")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "llm-check":
        from .analysis.models import PassAOutput
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
                user='只输出 {"characters": [], "relationships": []}',
                model_cls=PassAOutput,
                pass_name="check",
                book_id="",
            )
        except (LLMError, LlmJsonError) as exc:
            print(f"LLM 不可用：{exc}")
            return 2
        print(f"LLM 可用：{settings.llm_base_url} / {settings.llm_model} → {result.model_dump()}")
        return 0

    if args.cmd == "run":
        from .pipeline import resume_book

        conn = connect(settings.db_path)
        init_db(conn)
        plan = resume_book(settings, conn, args.book_id)
        text = "、".join(f"{kind}#{chapter}" for kind, chapter in plan) or "（无，全部已完成）"
        print(f"入队：{text}")
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

        conn = connect(settings.db_path)
        init_db(conn)
        uvicorn.run(create_app(settings, conn), host=args.host, port=args.port)
        return 0

    if args.cmd == "worker":
        from .engines.fake import FakeEngine
        from .handlers import casting, characters, lines, post, scenes, split, synthesize  # noqa: F401
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
            engine=FakeEngine(),
            llm=llm,
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
