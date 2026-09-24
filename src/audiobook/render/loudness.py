import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import run_ffmpeg

JSON_BLOCK = re.compile(r"\{.*?\}", re.S)
MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
MAX_VOLUME = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


@dataclass(frozen=True)
class LoudnessResult:
    mode: str
    target: float
    measured_before: float | None = None
    true_peak_before: float | None = None
    applied_gain_db: float | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "target": self.target,
            "measured_before": self.measured_before,
            "true_peak_before": self.true_peak_before,
            "applied_gain_db": self.applied_gain_db,
        }


def _first_json_block(text: str) -> dict:
    for match in JSON_BLOCK.finditer(text):
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if "input_i" in payload:
            return payload
    raise ValueError(f"loudnorm 没有输出可解析的 JSON：{text[-300:]}")


def measure_loudness(settings, src: Path) -> dict:
    output = run_ffmpeg(
        settings,
        [
            "-i",
            str(src),
            "-af",
            f"loudnorm=I={settings.loudness_target_lufs}:TP={settings.loudness_true_peak}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        loglevel="info",
    )
    return _first_json_block(output)


def measure_rms(settings, src: Path) -> tuple[float, float]:
    output = run_ffmpeg(settings, ["-i", str(src), "-af", "volumedetect", "-f", "null", "-"], loglevel="info")
    mean = MEAN_VOLUME.search(output)
    peak = MAX_VOLUME.search(output)
    if not mean or not peak:
        raise ValueError(f"volumedetect 没有输出可解析的统计：{output[-300:]}")
    return float(mean.group(1)), float(peak.group(1))


def _output_args(dst: Path, sample_rate: int, channels: int) -> list[str]:
    return ["-ar", str(sample_rate), "-ac", str(channels), "-c:a", "pcm_s16le", str(dst)]


def normalize_to_file(settings, src: Path, dst: Path, *, sample_rate: int, channels: int = 1) -> LoudnessResult:
    mode = (settings.loudness_mode or "off").lower()
    if mode == "off":
        Path(dst).write_bytes(Path(src).read_bytes())
        return LoudnessResult(mode="off", target=0.0)
    if mode == "rms":
        mean, peak = measure_rms(settings, src)
        gain = settings.loudness_rms_target_db - mean
        gain = min(gain, settings.loudness_true_peak - peak)
        run_ffmpeg(
            settings,
            ["-i", str(src), "-af", f"volume={gain:.2f}dB", *_output_args(dst, sample_rate, channels)],
        )
        return LoudnessResult(
            mode="rms",
            target=settings.loudness_rms_target_db,
            measured_before=mean,
            true_peak_before=peak,
            applied_gain_db=round(gain, 3),
        )
    if mode != "lufs":
        raise ValueError(f"未知响度模式：{settings.loudness_mode}（可选 lufs | rms | off）")
    measured = measure_loudness(settings, src)
    chain = (
        f"loudnorm=I={settings.loudness_target_lufs}:TP={settings.loudness_true_peak}:LRA=11"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true"
    )
    run_ffmpeg(settings, ["-i", str(src), "-af", chain, *_output_args(dst, sample_rate, channels)])
    return LoudnessResult(
        mode="lufs",
        target=settings.loudness_target_lufs,
        measured_before=float(measured["input_i"]),
        true_peak_before=float(measured["input_tp"]),
    )
