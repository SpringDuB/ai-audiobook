from audiobook.analysis.characters import (
    NARRATOR_ID,
    aggregate_characters,
    characters_index,
    relationships_index,
    resolve_speaker,
    role_name,
)
from audiobook.analysis.models import PassAOutput


def _out(characters, relationships=None) -> PassAOutput:
    return PassAOutput.model_validate({"characters": characters, "relationships": relationships or []})


CH1 = _out(
    [
        {"name": "苏锐", "aliases": ["老苏"], "gender": "男", "age_group": "青年", "personality": ["冷静"]},
        {"name": "王胖子", "aliases": [], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    [
        {"from": "苏锐", "to": "王胖子", "closeness": 0.8, "hierarchy": 0.2, "hostility": 0.0, "intimacy": 0.7,
         "note": "死党"},
        {"from": "苏锐", "to": "不存在的人", "hostility": 0.5},
        {"from": "王胖子", "to": "王胖子", "hostility": 0.5},
    ],
)
CH2 = _out(
    [
        {"name": "锐哥", "aliases": ["苏锐"], "gender": "男", "age_group": "青年", "personality": ["果断"]},
        {"name": "王胖子", "aliases": ["胖子"], "gender": "男", "age_group": "中年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    [
        {"from": "苏锐", "to": "胖子", "closeness": 0.6, "hierarchy": 0.2, "hostility": 0.4, "intimacy": 0.5},
    ],
)


def test_merges_aliases_into_one_role():
    payload = aggregate_characters([(1, CH1), (2, CH2)])
    names = {c["name"]: c for c in payload["characters"]}
    assert set(names) == {"旁白", "苏锐", "王胖子"}
    assert names["苏锐"]["aliases"] == ["老苏", "锐哥"]
    assert names["王胖子"]["aliases"] == ["胖子"]


def test_role_ids_are_stable_and_narrator_is_fixed():
    payload = aggregate_characters([(1, CH1), (2, CH2)])
    assert payload["characters"][0]["id"] == NARRATOR_ID
    assert [c["id"] for c in payload["characters"][1:]] == ["role_0001", "role_0002"]
    assert payload["characters"][1]["name"] == "苏锐"
    assert payload["characters"][1]["chapters"] == [1, 2]
    assert payload["characters"][1]["mentions"] == 2
    assert payload["characters"][1]["personality"] == ["冷静", "果断"]


def test_majority_vote_ignores_unknown_and_keeps_first_on_tie():
    payload = aggregate_characters([(1, CH1), (2, CH2)])
    wang = characters_index(payload)["role_0002"]
    assert wang["gender"] == "男"
    assert wang["age_group"] == "青年"


def test_relationships_are_averaged_and_bad_ones_dropped():
    payload = aggregate_characters([(1, CH1), (2, CH2)])
    rels = relationships_index(payload)
    assert len(rels) == 1
    rel = rels[("role_0001", "role_0002")]
    assert rel["closeness"] == 0.7
    assert rel["hostility"] == 0.2
    assert rel["intimacy"] == 0.6
    assert rel["chapters"] == [1, 2]
    assert rel["note"] == "死党"
    assert payload["dropped_relationships"] == 2


def test_injects_narrator_when_missing():
    payload = aggregate_characters([(1, _out([{"name": "苏锐", "gender": "男"}]))])
    narrator = characters_index(payload)[NARRATOR_ID]
    assert narrator["name"] == "旁白"
    assert narrator["is_narrator"] is True
    assert narrator["chapters"] == []


def test_resolve_speaker_handles_names_aliases_and_unknown():
    payload = aggregate_characters([(1, CH1), (2, CH2)])
    assert resolve_speaker(payload, "苏锐") == "role_0001"
    assert resolve_speaker(payload, " 老苏 ") == "role_0001"
    assert resolve_speaker(payload, "锐哥") == "role_0001"
    assert resolve_speaker(payload, "黑衣人") is None
    assert role_name(payload, "role_0002") == "王胖子"
    assert role_name(payload, "narrator") == "旁白"
