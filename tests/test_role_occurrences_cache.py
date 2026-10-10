"""选角页的角色出场统计缓存：一整本书 2.3 万行只该解析一次，文件变了立刻重算。"""

from audiobook import store
from audiobook.api.app import _role_occurrences


def test_role_occurrences_cache_tracks_new_lines_and_keeps_its_copy(settings, narrator_lines):
    store.write_jsonl_atomic(store.lines_path(settings, "b8", 0), narrator_lines(0, "第一句。"))
    first = _role_occurrences(settings, "b8")
    assert first["narrator"] == {"chapters": [0], "lines": 1}

    # 返回值是副本：外部改它不该污染缓存
    first["narrator"]["chapters"].append(99)
    first["narrator"]["lines"] = 999
    assert _role_occurrences(settings, "b8")["narrator"] == {"chapters": [0], "lines": 1}

    # 新增一章的逐句标注：签名变了，缓存必须失效
    store.write_jsonl_atomic(store.lines_path(settings, "b8", 1), narrator_lines(1, "第二句。第三句。"))
    updated = _role_occurrences(settings, "b8")
    assert updated["narrator"]["chapters"] == [0, 1]
    assert updated["narrator"]["lines"] == 3


def test_role_occurrences_counts_speaker_only_for_lines(settings, narrator_lines):
    rows = narrator_lines(0, "第一句。")
    rows[0]["addressee"] = "role_0001"
    rows[0]["addressee_name"] = "小鹿"
    store.write_jsonl_atomic(store.lines_path(settings, "b9", 0), rows)

    occurrences = _role_occurrences(settings, "b9")
    assert occurrences["narrator"]["lines"] == 1
    # 受话人也算"在这一章出场"，但不计台词数
    assert occurrences["role_0001"] == {"chapters": [0], "lines": 0}
