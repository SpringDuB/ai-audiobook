import pytest
from pydantic import ValidationError

from audiobook.analysis.models import ExtractionOutput, MergeOutput, RecommendOutput, SpokenLine


def test_spoken_line_normalizes_missing_role_and_unknown_emotion():
    line = SpokenLine.model_validate({"text": "  走。 ", "role": "  ", "emotion": "不存在的情绪", "intensity": 3})
    assert line.text == "走。"
    assert line.role == "未知"
    assert line.emotion is None
    assert line.intensity == 1.0


def test_extraction_output_must_be_a_json_array():
    out = ExtractionOutput.model_validate_json('[{"text":"走。","role":"苏锐","emotion":"愤怒","intensity":0.8}]')
    assert out.root[0].role == "苏锐"
    assert out.root[0].emotion == "愤怒"
    with pytest.raises(ValidationError):
        ExtractionOutput.model_validate_json('{"lines":[]}')


def test_spoken_line_ignores_extra_fields_so_one_bad_row_does_not_kill_the_window():
    line = SpokenLine.model_validate({"text": "走。", "role": "苏锐", "index": 3})
    assert line.text == "走。" and line.role == "苏锐"


def test_merge_output_accepts_a_single_alias_string():
    out = MergeOutput.model_validate({"characters": [{"name": "苏锐", "aliases": "老苏"}]})
    assert out.characters[0].name == "苏锐"
    assert out.characters[0].aliases == ["老苏"]


def test_voice_recommendation_uses_camel_case_voice_id_and_clamps_confidence():
    out = RecommendOutput.model_validate(
        {"recommendations": [{"voiceId": "v001", "confidence": 1.5, "reason": "冷静克制"}]}
    )
    rec = out.recommendations[0]
    assert rec.voice_id == "v001"
    assert rec.confidence == 1.0
    assert rec.model_dump(by_alias=True)["voiceId"] == "v001"
