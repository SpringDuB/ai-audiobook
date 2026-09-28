import sqlite3
from pathlib import Path

import pytest

from audiobook.analysis.derive import derive_line
from audiobook.config import get_settings
from audiobook.db import connect, init_db
from audiobook.text.split_chapters import split_sentences


def make_narrator_lines(chapter_index: int, text: str, scene_index: int = 1) -> list[dict]:
    """测试用行工厂：整章一个场景、全部旁白（字段与真实分析结果完全一致）。"""
    rows: list[dict] = []
    for seq, sentence in enumerate(split_sentences(text), start=1):
        rows.append(
            derive_line(
                {"emotion": None, "intensity": None, "delivery": "normal"},
                chapter_index=chapter_index,
                scene_index=scene_index,
                seq=seq,
                sentence=sentence,
                speaker_id="narrator",
                speaker_name="旁白",
                addressee_id=None,
                addressee_name=None,
                character=None,
                relationship=None,
                pronounce_table={},
            )
        )
    return rows


@pytest.fixture()
def settings(tmp_path: Path):
    # 单元测试默认关掉响度归一与容器封装：不依赖 ffmpeg，时长断言保持精确。
    # 需要真实 ffmpeg 行为的用例用 settings.model_copy(update={...}) 显式打开。
    return get_settings(data_dir=tmp_path / "data", loudness_mode="off", export_mkv=False)


@pytest.fixture()
def conn(settings) -> sqlite3.Connection:
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def narrator_lines():
    return make_narrator_lines
