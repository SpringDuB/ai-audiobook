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
    # 逐行合成会产生成百上千次请求，默认关掉访问日志，需要排错时加 --access-log
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=args.access_log)
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


def _download(args) -> int:
    from .download import ensure_model

    settings = get_settings()
    if args.source:
        settings = settings.model_copy(update={"model_source": args.source})
    if args.model_dir:
        settings = settings.model_copy(update={"model_dir": args.model_dir})
    if args.hf_endpoint:
        settings = settings.model_copy(update={"hf_endpoint": args.hf_endpoint})
    if args.model_id:
        settings = settings.model_copy(update={"model_id": args.model_id})
    result = ensure_model(settings)
    print(f"model_dir={result['path']}")
    print(f"source={result['source']} verified={result['verified']}")
    for warning in result["warnings"]:
        print(f"警告：{warning}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aiab-tts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="启动 TTS 服务")
    serve.add_argument("--backend", default=None, choices=["fake", "indextts"])
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--access-log", action="store_true", help="打开逐请求访问日志（默认关闭）")

    check = sub.add_parser("check", help="检查运行中的 TTS 服务")
    check.add_argument("--url", default="http://127.0.0.1:8020")

    unload = sub.add_parser("unload", help="释放运行中服务的模型/显存")
    unload.add_argument("--url", default="http://127.0.0.1:8020")

    download = sub.add_parser("download", help="按来源下载/校验模型权重")
    download.add_argument("--source", default=None, choices=["modelscope", "huggingface", "local"])
    download.add_argument("--model-dir", default=None)
    download.add_argument("--model-id", default=None)
    download.add_argument("--hf-endpoint", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "check":
        return _check(args)
    if args.cmd == "unload":
        return _unload(args)
    if args.cmd == "download":
        return _download(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
