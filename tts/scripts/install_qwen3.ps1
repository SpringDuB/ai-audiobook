# 安装 Qwen3-TTS 后端（VoiceDesign + Base）：独立 venv + 权重下载。
#
# 与 IndexTTS 的 venv 分开是故意的：
#   - indextts 钉在 transformers 4.52.1 / python 3.11；
#   - qwen-tts 钉在 transformers 4.57.3 / python 3.12；
#   两套依赖塞进同一个 venv 会互相拆台。
#
# 用法（在 tts/ 目录下）：
#   powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/install_qwen3.ps1 -SkipModels   # 只建环境

param(
    [string]$VenvName = ".venv-qwen",
    [string]$ModelDir = "checkpoints",
    # 走国内镜像：直连 files.pythonhosted.org 经常超时（实测 60s 就重试报错）
    [string]$IndexUrl = "https://mirrors.aliyun.com/pypi/simple",
    # Windows 上 PyPI 的 torch 是 CPU 版，CUDA 版只能从 pytorch 的 wheel 源装
    [string]$TorchIndexUrl = "https://mirrors.aliyun.com/pytorch-wheels/cu128/",
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
$TtsDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $TtsDir $VenvName
$Python = Join-Path $VenvDir "Scripts/python.exe"

Write-Host "[1/3] 创建独立 venv（Python 3.12）: $VenvDir"
uv venv --python 3.12 $VenvDir

Write-Host "[2/3] 安装 qwen-tts 与推理依赖"
# 先装 aiab-tts 自身（可编辑），它会带上 fastapi/uvicorn/pydantic 这些基础依赖
uv pip install --python $Python --index-url $IndexUrl -e $TtsDir
uv pip install --python $Python --index-url $IndexUrl `
    "qwen-tts==0.1.1" "modelscope>=1.9" "soundfile" "librosa"
# torch 必须从 CUDA 源装：PyPI 上的是 CPU 版（跑起来会报 "Torch not compiled with CUDA enabled"）
uv pip install --python $Python --index-url $IndexUrl --find-links $TorchIndexUrl `
    "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128"

if ($SkipModels) {
    Write-Host "跳过权重下载（-SkipModels）"
    exit 0
}

Write-Host "[3/3] 下载模型权重到 $ModelDir"
foreach ($Model in @("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")) {
    $Name = $Model.Split("/")[-1]
    $Target = Join-Path $TtsDir (Join-Path $ModelDir $Name)
    Write-Host "  -> $Name"
    & $Python -c "from modelscope import snapshot_download; snapshot_download('$Model', local_dir=r'$Target')"
}

Write-Host "完成。启动：uv run --project tts aiab-tts serve --backend qwen3"
