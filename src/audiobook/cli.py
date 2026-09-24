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

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

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
        from .handlers import post, split, synthesize  # noqa: F401  导入即注册
        from .worker import WorkerContext, run_forever, run_once

        conn = connect(settings.db_path)
        init_db(conn)
        ctx = WorkerContext(
            settings=settings,
            conn=conn,
            worker_id=args.worker_id or f"w-{os.getpid()}-{uuid.uuid4().hex[:6]}",
            engine=FakeEngine(),
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
