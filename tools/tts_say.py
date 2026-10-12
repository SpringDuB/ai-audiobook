"""给一段文本 + 一段音色描述，直接调本机 TTS 服务（默认 8020）生成音频并落盘。

用法（在项目根目录跑，用 uv run 才能拿到项目环境里的 httpx / audiobook 包）：
    uv run python tools/tts_say.py \
        --voice "二十多岁的年轻女性，中音区，音色清亮，吐字清楚" \
        --text "你、你别过来……" \
        --out data/tmp/say.wav

    # 文本或描述很长、或者命令行里有中文不好传时，从文件读（UTF-8）
    uv run python tools/tts_say.py --voice-file voice.txt --text-file lines.txt \
        --out data/tmp/say.wav --play

说明：
  - 走 /v1/synthesize 的 voicePrompt 通道：文本是"这一句说什么"，音色描述是
    "这个声音天生是什么样"（和正式合成时喂给模型的东西一致）。
  - 文本超过服务端 maxTextChars 时按句子边界分块，逐块合成后拼回一个 wav
    （块之间插 --pause-ms 毫秒静音，跟合成链里的做法一致）。
  - 只调用本机服务：不读数据库、不写成品目录，输出路径你想给哪就给哪。
  - 服务刚起来、模型还没加载时，第一条请求会等模型加载（首次可能要一两分钟），
    所以默认超时给得比较宽（--timeout，默认 600 秒）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

from audiobook import audio
from audiobook.text.chunking import chunk_text

DEFAULT_ENDPOINT = "http://127.0.0.1:8020"
DEFAULT_PAUSE_MS = 120


def _read_text(value: str | None, path: str | None, label: str, flag: str) -> str:
    if value and path:
        raise SystemExit(f"{label}只能给一个：--{flag} 或 --{flag}-file")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    if value:
        return value.strip()
    raise SystemExit(f"缺少{label}：--{flag} 或 --{flag}-file")


def _error_message(response: httpx.Response) -> str:
    """把服务端的 {"detail": {...}} 错误体翻译成一行能看的提示。"""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text[:200]
    if isinstance(detail, dict):
        return f"{detail.get('code', 'error')}: {detail.get('message', '')}"
    return str(detail or response.reason_phrase)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # 中文在 Windows 控制台上不至于变成乱码
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        description="按音色描述生成一句/一段试听音频（调用本机 TTS 服务）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--text", help="要合成的文本")
    parser.add_argument("--text-file", help="从 UTF-8 文本文件读要合成的文本")
    parser.add_argument("--voice", help="音色描述，例如“四十岁上下的男性，中低音区，嗓音厚实”")
    parser.add_argument("--voice-file", help="从 UTF-8 文本文件读音色描述")
    parser.add_argument("-o", "--out", required=True, help="输出 wav 路径（目录不存在会自动建）")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="TTS 服务地址")
    parser.add_argument("--lang", default="ZH", help="语言代码")
    parser.add_argument("--rate", type=float, default=1.0, help="语速倍率（0.5–2.0）")
    parser.add_argument("--seed", type=int, help="固定随机种子（同样的输入复现同一版声音）")
    parser.add_argument("--pause-ms", type=int, default=DEFAULT_PAUSE_MS, help="分块之间的静音毫秒数")
    parser.add_argument("--max-chars", type=int, help="每块的字符上限（默认取服务端 maxTextChars）")
    parser.add_argument("--no-chunk", action="store_true", help="不做分块（文本超长会直接被服务端拒绝）")
    parser.add_argument("--timeout", type=float, default=600.0, help="单条请求超时（秒）")
    parser.add_argument("--play", action="store_true", help="生成后用系统默认播放器打开")
    args = parser.parse_args(argv)

    text = _read_text(args.text, args.text_file, "文本", "text")
    voice = _read_text(args.voice, args.voice_file, "音色描述", "voice")
    if not text:
        raise SystemExit("文本是空的")
    if not voice:
        raise SystemExit("音色描述是空的")
    if len(voice) > 400:
        # 服务端 schemas 里 voicePrompt ≤ 400 字，先在这里拦一下，省一次往返
        raise SystemExit(f"音色描述太长了（{len(voice)} 字，上限 400）")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint.rstrip("/")

    # trust_env=False：本机回环不走系统代理（开着代理的机器上，代理会替 127.0.0.1 应答）
    with httpx.Client(
        base_url=endpoint, timeout=httpx.Timeout(args.timeout, connect=10.0), trust_env=False
    ) as client:
        try:
            health_response = client.get("/health")
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"连不上 TTS 服务（{endpoint}）：{exc}\n"
                "先在设置页点「一键启动 TTS 服务」把它拉起来；确认端口没被别的程序占着。"
            ) from exc
        try:
            health = health_response.json()
        except ValueError as exc:
            snippet = health_response.text.strip()[:120] or "(空响应)"
            raise SystemExit(
                f"{endpoint} 上不是 TTS 服务或它没起来：HTTP {health_response.status_code} · {snippet}\n"
                "设置页点「一键启动 TTS 服务」；如果端口被别的程序占着，先把它停掉。"
            ) from exc
        try:
            caps = client.get("/capabilities").json()
        except (httpx.HTTPError, ValueError):
            caps = {}  # 老版本服务没有能力表也能跑，后面用默认值

        engine = f"{health.get('engine', '?')} {health.get('engineVersion', '')}".strip()
        loaded = "已加载" if health.get("modelLoaded") else "未加载（第一条请求会顺带把模型加载起来）"
        print(f"服务：{endpoint} · {engine} · 模型{loaded}")
        if not caps.get("voiceDesign", True):
            print("提示：这个后端的能力表里没有 voiceDesign，voicePrompt 可能不生效。")

        limit = int(args.max_chars or caps.get("maxTextChars") or 300)
        if args.no_chunk or len(text) <= limit:
            chunks = [text]
        else:
            chunks = chunk_text(text, limit) or [text]

        parts: list[tuple[Path, int]] = []
        started = time.perf_counter()
        try:
            for position, chunk in enumerate(chunks, start=1):
                payload: dict = {
                    "text": chunk,
                    "lang": args.lang,
                    "rate": args.rate,
                    "format": "wav",
                    "voicePrompt": voice,
                }
                if args.seed is not None:
                    payload["seed"] = args.seed

                request_started = time.perf_counter()
                response = client.post("/v1/synthesize", json=payload)
                if response.status_code >= 400:
                    raise SystemExit(f"合成失败（HTTP {response.status_code}）：{_error_message(response)}")

                if len(chunks) == 1:
                    part_path = out_path
                else:
                    part_path = out_path.with_name(f"{out_path.stem}.part{position:02d}.wav")
                part_path.write_bytes(response.content)
                seconds = float(response.headers.get("X-Duration-Sec") or 0.0) or audio.wav_duration(part_path)
                parts.append((part_path, args.pause_ms if position < len(chunks) else 0))
                print(
                    f"  第 {position}/{len(chunks)} 块：{len(chunk)} 字 → "
                    f"{seconds:.2f}s 音频（{time.perf_counter() - request_started:.1f}s）"
                )

            if len(parts) > 1:
                duration = audio.concat_with_pauses(parts, out_path)
            else:
                duration = audio.wav_duration(out_path)
        finally:
            for part_path, _ in parts:
                if part_path != out_path:
                    part_path.unlink(missing_ok=True)

        elapsed = time.perf_counter() - started

    size_mb = out_path.stat().st_size / 1e6
    print(
        f"完成：{out_path}（{duration:.2f}s，{size_mb:.2f} MB，"
        f"{len(chunks)} 块，用时 {elapsed:.1f}s，采样率 {caps.get('sampleRate', '?')}）"
    )
    if args.play:
        if os.name == "nt":
            os.startfile(out_path)  # noqa: S606 - 本机试听用
        else:
            print("（--play 只在 Windows 上自动打开，其它系统请手动播放）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
