import pytest
from pydantic import ValidationError

from audiobook.analysis.models import CharacterCard, PassAOutput, Relationship


def test_character_card_rejects_unknown_fields_and_missing_name():
    with pytest.raises(ValidationError):
        CharacterCard.model_validate({"name": "秦风", "unknown_key": 1})
    with pytest.raises(ValidationError):
        CharacterCard.model_validate({"aliases": ["秦少"]})


def test_character_card_is_lenient_about_optional_details():
    card = CharacterCard.model_validate({"name": "秦风", "aliases": None, "personality": "冷酷"})
    assert card.aliases == []
    assert card.personality == ["冷酷"]
    assert card.speaking_style == ""
    assert card.base_intensity == 0.4


def test_character_card_clamps_intensity():
    assert CharacterCard.model_validate({"name": "X", "base_intensity": 3.5}).base_intensity == 1.0
    assert CharacterCard.model_validate({"name": "X", "base_intensity": -1}).base_intensity == 0.0


def test_relationship_uses_from_to_in_json_and_named_fields_in_python():
    rel = Relationship.model_validate(
        {"from": "秦风", "to": "张卫东", "closeness": 0.2, "hierarchy": 0.1, "hostility": 0.9, "intimacy": 0.0}
    )
    assert rel.source == "秦风" and rel.target == "张卫东"
    dumped = rel.model_dump(by_alias=True)
    assert dumped["from"] == "秦风" and dumped["to"] == "张卫东"
    assert Relationship(source="A", target="B").closeness == 0.5


def test_pass_a_output_requires_both_lists():
    with pytest.raises(ValidationError):
        PassAOutput.model_validate({"characters": []})
    ok = PassAOutput.model_validate_json('{"characters": [], "relationships": []}')
    assert ok.characters == [] and ok.relationships == []
