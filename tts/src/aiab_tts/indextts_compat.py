"""index-tts 上游的小补丁：补齐 HuggingFace → ModelScope 的仓库别名。

index-tts 会把辅助模型的 HuggingFace repo id 直接拿去 ModelScope 查，但两边的命名
并不总是一样：BigVGAN 在 ModelScope 上属于 ``nv-community`` 而不是 ``nvidia``。
少了这条映射，每次启动都会先 404 一次、再退回 hf-mirror 兜底——国内这条路很慢，
449MB 的声码器能拖几个小时，看起来就像卡住了。

补丁写在我们的仓库里（``tts/index-tts`` 是 gitignore 的第三方目录，改那儿重装就丢）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# HuggingFace repo id -> ModelScope model id（逐个在 modelscope.cn 上核对过存在性）
MODELSCOPE_REPO_ALIASES: dict[str, str] = {
    "nvidia/bigvgan_v2_22khz_80band_256x": "nv-community/bigvgan_v2_22khz_80band_256x",
    "nvidia/bigvgan_v2_24khz_100band_256x": "nv-community/bigvgan_v2_24khz_100band_256x",
    "nvidia/bigvgan_v2_44khz_128band_256x": "nv-community/bigvgan_v2_44khz_128band_256x",
    "nvidia/bigvgan_v2_44khz_128band_512x": "nv-community/bigvgan_v2_44khz_128band_512x",
}


def modelscope_repo_id(repo_id: str) -> str:
    """把 HuggingFace 名字换成 ModelScope 上真存在的名字；没有别名就原样返回。"""
    return MODELSCOPE_REPO_ALIASES.get(repo_id, repo_id)


def apply_modelscope_aliases() -> list[str]:
    """把别名补进 index-tts 的 ``HF_TO_MODELSCOPE_REPO_MAP``，返回本次新补的 repo id。

    幂等：已经在表里的不再重复写。index-tts 没装、或上游以后换了映射表结构，就静默
    跳过——下载本身还有 hf-mirror 兜底，不该因为补丁失败而拦住服务启动。
    """
    try:
        from indextts.utils import model_download
    except Exception:  # noqa: BLE001 - 上游模块结构变了也不该炸
        logger.debug("没找到 indextts.utils.model_download，跳过 ModelScope 别名补丁")
        return []

    mapping = getattr(model_download, "HF_TO_MODELSCOPE_REPO_MAP", None)
    if not isinstance(mapping, dict):
        logger.debug("index-tts 的 HF_TO_MODELSCOPE_REPO_MAP 结构变了，跳过别名补丁")
        return []

    patched = [hf_id for hf_id, ms_id in MODELSCOPE_REPO_ALIASES.items() if mapping.get(hf_id) != ms_id]
    for hf_id in patched:
        mapping[hf_id] = MODELSCOPE_REPO_ALIASES[hf_id]
    if patched:
        logger.info("已补上 %d 条 ModelScope 仓库别名：%s", len(patched), "、".join(patched))
    return patched
