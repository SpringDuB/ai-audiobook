import argparse
import sys

from .config import get_settings


def describe_backend(settings) -> list[str]:
    """启动时先说清楚"要不要模型、要不要下载"，免得日志里只有 uvicorn 三行。"""
    from pathlib import Path

    backend = (settings.backend or "indextts").lower()
    model_dir = Path(settings.model_dir).resolve()
    lines = [f"后端 {backend}：模型来源={settings.model_source}，模型目录={model_dir}"]
    if settings.model_source in {"modelscope", "huggingface"}:
        lines.append("权重缺失/损坏时会按这个来源自动下载，进度就打印在本日志里。")
    else:
        lines.append("模型来源 local：只做校验，不下载；缺失时会在这里直接报错。")
    return lines


def _serve(args) -> int:
    import logging

    import uvicorn

    from .app import build_state, create_app

    # 不配日志的话，我们自己 logger 的消息（模型就绪、辅助模型缺失…）根本不会出现在托管日志里
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if args.backend:
        settings = settings.model_copy(update={"backend": args.backend})
    if args.port:
        settings = settings.model_copy(update={"port": args.port})
    if args.host:
        settings = settings.model_copy(update={"host": args.host})
    print(f"启动 TTS 服务：backend={settings.backend} host={settings.host} port={settings.port}")
    for line in describe_backend(settings):
        print(line, flush=True)
    state = build_state(settings)
    app = create_app(settings, state)
    if not args.lazy:
        print("开始预加载模型（首次会先下载权重，可能要几分钟）…", flush=True)
        try:
            result = state.warmup()
        except Exception as exc:  # noqa: BLE001 - 起不来就带着日志退出，别留下一个假装健康的服务
            print(f"模型准备失败：{type(exc).__name__}: {exc}", flush=True)
            return 2
        print(f"模型已加载：{result}", flush=True)
    # 逐行合成会产生成百上千次请求，默认关掉访问日志，需要排错时加 --access-log
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=args.access_log)
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
    serve.add_argument("--backend", default=None, choices=["indextts"])
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--access-log", action="store_true", help="打开逐请求访问日志（默认关闭）")
    serve.add_argument("--lazy", action="store_true", help="不在启动时预加载模型（第一次合成时才加载）")

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
