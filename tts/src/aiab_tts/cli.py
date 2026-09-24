import argparse
import sys

from .config import get_settings


def _serve(args) -> int:
    import uvicorn

    from .app import create_app

    settings = get_settings()
    if args.backend:
        settings = settings.model_copy(update={"backend": args.backend})
    if args.port:
        settings = settings.model_copy(update={"port": args.port})
    if args.host:
        settings = settings.model_copy(update={"host": args.host})
    print(f"启动 TTS 服务：backend={settings.backend} host={settings.host} port={settings.port}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


def _check(args) -> int:
    import httpx

    base = args.url.rstrip("/")
    try:
        health = httpx.get(f"{base}/health", timeout=10.0).json()
        caps = httpx.get(f"{base}/capabilities", timeout=10.0).json()
    except Exception as exc:  # noqa: BLE001 - 诊断命令不抛栈
        print(f"TTS 服务不可用：{exc}")
        return 2
    print(f"engine={health.get('engine')} version={health.get('engineVersion')} status={health.get('status')}")
    print(f"并发自报={health.get('recommendedConcurrency')} inflight={health.get('inflight')}")
    print(f"情绪={caps.get('emotionDims')} 语速={caps.get('rateRange')} 语言={caps.get('languages')}")
    print(f"采样率={caps.get('sampleRate')} 单次文本上限={caps.get('maxTextChars')}")
    return 0


def _unload(args) -> int:
    import httpx

    try:
        payload = httpx.post(f"{args.url.rstrip('/')}/unload", timeout=60.0).json()
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接 TTS 服务：{exc}")
        return 2
    print(f"unload: {payload}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aiab-tts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="启动 TTS 服务")
    serve.add_argument("--backend", default=None, choices=["fake", "indextts"])
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    check = sub.add_parser("check", help="检查运行中的 TTS 服务")
    check.add_argument("--url", default="http://127.0.0.1:8020")

    unload = sub.add_parser("unload", help="释放运行中服务的模型/显存")
    unload.add_argument("--url", default="http://127.0.0.1:8020")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "check":
        return _check(args)
    if args.cmd == "unload":
        return _unload(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
