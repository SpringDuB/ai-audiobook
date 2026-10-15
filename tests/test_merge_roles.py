"""角色整合：把「同一个人被叫了好几个名字」交给大模型合并，代码只做校验与 id 分配。"""

from audiobook.analysis.merge import extend_characters, merge_roles, role_entries
from audiobook.analysis.models import SpokenLine
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

CHAPTER_ONE = [
    SpokenLine(text="苏锐看着他。", role="旁白"),
    SpokenLine(text="老苏，你怎么看？", role="王胖子", emotion="平静", intensity=0.4),
    SpokenLine(text="不急。", role="苏锐", emotion="平静", intensity=0.3),
]
CHAPTER_TWO = [
    SpokenLine(text="锐哥来了。", role="王胖子", emotion="喜悦", intensity=0.5),
    SpokenLine(text="来了。", role="锐哥", emotion="平静", intensity=0.3),
]

MERGED = {
    "characters": [
        {"name": "苏锐", "aliases": ["老苏", "锐哥"]},
        {"name": "王胖子", "aliases": ["胖子"]},
    ]
}


def _runner(settings, llm):
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings)


def test_role_entries_collect_counts_chapters_and_samples():
    entries = role_entries([(1, CHAPTER_ONE), (2, CHAPTER_TWO)])
    by_name = {entry["name"]: entry for entry in entries}
    assert set(by_name) == {"苏锐", "王胖子", "锐哥"}
    assert by_name["王胖子"]["count"] == 2
    assert by_name["王胖子"]["chapters"] == [1, 2]
    assert by_name["苏锐"]["samples"][0] == "不急。"


def test_merge_roles_uses_llm_mapping_and_assigns_stable_ids(settings):
    llm = FakeLLM(routes={"【MERGE_ROLES】": MERGED})
    payload, issues = merge_roles(
        _runner(settings, llm),
        book_id="b1",
        entries=role_entries([(1, CHAPTER_ONE), (2, CHAPTER_TWO)]),
        enabled=True,
    )
    assert issues == []
    assert "【MERGE_ROLES】" in llm.calls[0]["user"]
    assert payload["characters"][0]["id"] == "narrator"
    # 展示顺序按「出现章数 → 首次出场」排；role_id 按整合结果的先后发
    assert [c["name"] for c in payload["characters"]] == ["旁白", "王胖子", "苏锐"]
    su_rui = next(c for c in payload["characters"] if c["name"] == "苏锐")
    # 只有清单里出现过的称呼才算数（"老苏""胖子" 这一章没出现，不能凭空写进来）
    assert su_rui["aliases"] == ["锐哥"]
    assert su_rui["chapters"] == [1, 2]
    assert payload["names"]["苏锐"] == "role_0001"
    assert payload["names"]["锐哥"] == "role_0001"
    assert payload["names"]["王胖子"] == "role_0002"
    assert payload["names"]["旁白"] == "narrator"


def test_merge_roles_falls_back_to_one_role_per_name_when_llm_fails(settings):
    llm = FakeLLM(routes={"【MERGE_ROLES】": MERGED}, fail_on={"【MERGE_ROLES】"})
    payload, issues = merge_roles(
        _runner(settings, llm),
        book_id="b1",
        entries=role_entries([(1, CHAPTER_ONE)]),
        enabled=True,
    )
    assert [issue["kind"] for issue in issues] == ["role_merge_failed"]
    names = [c["name"] for c in payload["characters"]]
    assert names == ["旁白", "王胖子", "苏锐"]
    assert payload["names"]["王胖子"] == "role_0001"
    assert payload["names"]["苏锐"] == "role_0002"


def test_merge_roles_keeps_names_the_model_forgot(settings):
    llm = FakeLLM(routes={"【MERGE_ROLES】": {"characters": [{"name": "苏锐", "aliases": ["老苏"]}]}})
    payload, issues = merge_roles(
        _runner(settings, llm),
        book_id="b1",
        entries=role_entries([(1, CHAPTER_ONE), (2, CHAPTER_TWO)]),
        enabled=True,
    )
    assert [issue["kind"] for issue in issues] == ["role_merge_incomplete"]
    assert issues[0]["detail"]["names"] == ["王胖子", "锐哥"]
    assert {c["name"] for c in payload["characters"]} == {"旁白", "苏锐", "王胖子", "锐哥"}


def test_extend_characters_maps_a_new_name_onto_an_existing_role(settings):
    entries = role_entries([(1, CHAPTER_ONE)])
    payload, _ = merge_roles(
        _runner(settings, FakeLLM(routes={"【MERGE_ROLES】": {"characters": [{"name": "苏锐", "aliases": ["老苏"]}]}})),
        book_id="b1",
        entries=entries,
        enabled=True,
    )
    llm = FakeLLM(routes={"【MERGE_ROLES】": {"characters": [{"name": "苏锐", "aliases": ["锐哥"]}]}})
    payload, issues = extend_characters(
        _runner(settings, llm),
        book_id="b1",
        payload=payload,
        spoken=[SpokenLine(text="锐哥来了。", role="锐哥", emotion="平静", intensity=0.3)],
    )
    assert issues == []
    assert "【MERGE_ROLES】" in llm.calls[0]["user"]
    assert payload["names"]["锐哥"] == "role_0001"
    assert len(payload["characters"]) == 3   # 旁白 + 苏锐 + 王胖子


def test_merge_roles_disabled_keeps_every_name_separate_without_llm(settings):
    """AB_ROLE_MERGE_ENABLED=false：每个称呼各自成角，一个合并请求都不发。"""
    llm = FakeLLM(routes={"【MERGE_ROLES】": MERGED})
    payload, issues = merge_roles(
        _runner(settings, llm),
        book_id="b1",
        entries=role_entries([(1, CHAPTER_ONE), (2, CHAPTER_TWO)]),
        enabled=False,
    )
    assert issues == []
    assert llm.calls == []
    names = [character["name"] for character in payload["characters"]]
    assert names[0] == "旁白"
    assert set(names[1:]) == {"王胖子", "苏锐", "锐哥"}
    assert all(character["aliases"] == [] for character in payload["characters"])
    # 锐哥 不再解析回 苏锐
    assert payload["names"]["锐哥"] != payload["names"]["苏锐"]


def test_merge_roles_disabled_reuses_existing_ids_and_drops_stale_aliases(settings):
    existing = [
        {"id": "narrator", "name": "旁白", "aliases": [], "chapters": [], "mentions": 0, "is_narrator": True},
        {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "chapters": [1], "mentions": 1, "is_narrator": False},
    ]
    payload, issues = merge_roles(
        None,
        book_id="b1",
        entries=role_entries([(1, CHAPTER_ONE)]),
        existing_characters=existing,
        enabled=False,
    )
    assert issues == []
    su = next(character for character in payload["characters"] if character["name"] == "苏锐")
    assert su["id"] == "role_0001" and su["aliases"] == []
    wang = next(character for character in payload["characters"] if character["name"] == "王胖子")
    assert wang["id"] == "role_0002"
    assert "老苏" not in payload["names"]


def test_extend_characters_disabled_does_not_pull_names_back_into_old_aliases(settings):
    """关合并时，新出现的称呼即使是老角色的别名，也要独立成角（不能又被并回去）。"""
    base = {
        "book_id": "b1",
        "characters": [
            {"id": "narrator", "name": "旁白", "aliases": [], "chapters": [], "mentions": 0, "is_narrator": True},
            {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "chapters": [1], "mentions": 1, "is_narrator": False},
        ],
        "names": {"旁白": "narrator", "苏锐": "role_0001", "老苏": "role_0001"},
    }
    payload, issues = extend_characters(
        _runner(settings, FakeLLM(routes={})),
        book_id="b1",
        payload=base,
        spoken=[SpokenLine(text="老苏来了。", role="老苏")],
        enabled=False,
    )
    assert issues == []
    assert payload["names"]["老苏"] == "role_0002"
    assert payload["names"]["苏锐"] == "role_0001"
    su = next(character for character in payload["characters"] if character["name"] == "苏锐")
    assert su["aliases"] == []
