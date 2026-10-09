"""一键生成全部角色试听：跳过已最新的、按描述生成、单个失败不拖垮整批。"""

import json

from audiobook import jobs, store
from audiobook.db import connect, init_db
from audiobook.handlers import voice_design as handler  # noqa: F401  导入即注册
from audiobook.handlers.voice_design import _preview_targets
from audiobook.worker import WorkerContext, run_once


def _seed(settings) -> str:
    book_id = "book1"
    store.atomic_replace_json(
        store.characters_path(settings, book_id),
        {
            "characters": [
                {"id": "narrator", "name": "旁白", "aliases": [], "is_narrator": True},
                {"id": "role_0001", "name": "苏锐", "aliases": []},
                {"id": "role_0002", "name": "林可", "aliases": []},
            ]
        },
    )
    store.atomic_replace_json(
        store.casting_path(settings, book_id),
        {
            "book_id": book_id,
            "roles": {
                "narrator": {
                    "role_id": "narrator",
                    "name": "旁白",
                    "voice_source": "design",
                    "description": "三十多岁的男性，中低音区，嗓音厚实。",
                    "sample": "夜色渐深。",
                },
                "role_0001": {
                    "role_id": "role_0001",
                    "name": "苏锐",
                    "voice_source": "design",
                    "description": "二十出头男性，中高音区，音色清亮。",
                    "sample": "我知道路。",
                },
                "role_0002": {
                    "role_id": "role_0002",
                    "name": "林可",
                    "voice_source": "library",
                    "source": "manual",
                    "voice_id": "v001",
                },
            },
        },
    )
    return book_id


class _Engine:
    """只实现 design_voice 的替身：记录调用，第二个角色故意失败。"""

    def __init__(self, fail_names=()):
        self.calls: list[str] = []
        self.fail_names = set(fail_names)

    def design_voice(self, *, instruct, text, lang, out_path, timeout=None):
        self.calls.append(instruct)
        if any(name in instruct for name in self.fail_names):
            raise RuntimeError("注入的引擎错误")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF0000WAVE")

        class _Result:
            duration = 1.5

        return _Result()


def _ctx(settings, conn, engine):
    return WorkerContext(settings=settings, conn=conn, worker_id="w1", engine=engine)


def test_preview_targets_skip_library_and_fresh(settings):
    book_id = _seed(settings)
    targets, described, skipped = _preview_targets(settings, book_id, {})

    # 手工绑库存音色的林可没有描述 → 不参与；旁白/苏锐都要生成
    assert [role_id for role_id, _ in targets] == ["narrator", "role_0001"]
    assert (described, skipped) == (2, 0)


def test_preview_targets_skip_current_preview(settings):
    book_id = _seed(settings)
    # 先按当前描述生成一份试听 → 再跑就该跳过它
    engine = _Engine()
    conn = connect(settings.db_path)
    init_db(conn)
    jobs.enqueue(conn, "voice_preview", book_id)
    assert run_once(_ctx(settings, conn, engine)) is True
    assert len(engine.calls) == 2

    targets, described, skipped = _preview_targets(settings, book_id, {})
    assert targets == []
    assert (described, skipped) == (2, 2)

    # force=True 时无视已有的试听
    forced, _, _ = _preview_targets(settings, book_id, {"force": True})
    assert len(forced) == 2


def test_preview_meta_written_with_description_key(settings):
    book_id = _seed(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    jobs.enqueue(conn, "voice_preview", book_id)
    assert run_once(_ctx(settings, conn, _Engine())) is True

    meta = json.loads(
        (store.role_voice_dir(settings, book_id, "role_0001") / "preview.json").read_text(encoding="utf-8")
    )
    assert meta["role_id"] == "role_0001"
    assert meta["sample"] == "我知道路。"
    assert meta["duration"] == 1.5
    assert meta["description_key"]


def test_one_role_failure_does_not_stop_the_batch(settings):
    book_id = _seed(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    jobs.enqueue(conn, "voice_preview", book_id)
    engine = _Engine(fail_names=("二十出头",))
    assert run_once(_ctx(settings, conn, engine)) is True

    assert len(engine.calls) == 2  # 两个都试过
    assert (store.role_voice_dir(settings, book_id, "narrator") / "preview.wav").exists()
    assert not (store.role_voice_dir(settings, book_id, "role_0001") / "preview.wav").exists()
    kinds = [row["kind"] for row in store.read_jsonl(store.issues_path(settings, book_id))]
    assert kinds == ["tts_line_failed"]
