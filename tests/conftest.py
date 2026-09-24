import sqlite3
from pathlib import Path

import pytest

from audiobook.config import get_settings
from audiobook.db import connect, init_db


@pytest.fixture()
def settings(tmp_path: Path):
    return get_settings(data_dir=tmp_path / "data")


@pytest.fixture()
def conn(settings) -> sqlite3.Connection:
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()
