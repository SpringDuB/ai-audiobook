"""选角登记：角色 → casting.json。角色默认走"设计音色"，手工绑库存音色的保持不动。"""

from audiobook.analysis.casting import (
    VoiceProfile,
    build_casting,
    load_voice_library,
    samples_by_role,
    voice_catalog_text,
    voice_for_speaker,
)
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner


def _voice(voice_id: str, **overrides) -> VoiceProfile:
    base = dict(
        id=voice_id, name=f"音色{voice_id}", gender="男", age_group="青年", personality=("冷酷",), genres=("都市",),
        mood=("沉稳",), speech_rate="中", voice_quality=("磁性",), language_style=("普通话",),
        usage_type=("角色对话",), description="冷酷而沉稳的男声",
    )
    base.update(overrides)
    return VoiceProfile(**base)


CHARACTERS = {
    "characters": [
        {"id": "narrator", "name": "旁白", "aliases": [], "is_narrator": True, "chapters": [1]},
        {"id": "role_0001", "name": "苏锐", "aliases": ["老苏"], "is_narrator": False, "chapters": [1]},
    ]
}

SAMPLES = {"narrator": ["夜色很深。"], "role_0001": ["不急。", "别废话。"]}


def _runner(settings, llm):
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings)


def _route(user: str) -> dict:
    """旁白拿 v_nar，其余角色拿 v_hero，并带一个不存在的 id 试校验。"""
    if "角色：旁白" in user:
        return {"recommendations": [{"voiceId": "v_nar", "confidence": 0.9, "reason": "适合旁白"}]}
    return {
        "recommendations": [
            {"voiceId": "不存在", "confidence": 0.99, "reason": "编的"},
            {"voiceId": "v_hero", "confidence": 0.8, "reason": "冷峻克制"},
            {"voiceId": "v_nar", "confidence": 0.4, "reason": "备选"},
        ]
    }


def test_voice_catalog_lists_every_voice_with_its_id_and_tags():
    text = voice_catalog_text([_voice("v001"), _voice("v002", name="优雅女设计师", gender="女")])
    assert "v001" in text and "音色v001" in text
    assert "v002" in text and "优雅女设计师" in text and "女" in text


def test_samples_by_role_prefers_dialogue_and_keeps_order():
    lines = {
        1: [
            {"speaker_name": "苏锐", "kind": "dialogue", "text": "别废话。"},
            {"speaker_name": "旁白", "kind": "narration", "text": "夜色很深。"},
            {"speaker_name": "苏锐", "kind": "dialogue", "text": "不急。"},
        ]
    }
    samples = samples_by_role(lines)
    assert samples["苏锐"] == ["别废话。", "不急。"]
    assert samples["旁白"] == ["夜色很深。"]


def test_build_casting_registers_every_role_as_design(settings):
    """所有角色（含旁白）默认登记成设计音色：不调大模型、不等音色库。"""
    voices = [_voice("v_nar", gender="女", usage_type=("旁白叙述",)), _voice("v_hero")]
    llm = FakeLLM(routes={"【VOICE_RECOMMEND】": _route})
    casting, issues = build_casting(
        _runner(settings, llm),
        book_id="b1",
        characters=CHARACTERS,
        samples=SAMPLES,
        voices=voices,
    )
    assert issues == []
    assert llm.calls == []  # 登记不花大模型
    for role in casting["roles"].values():
        assert role["voice_source"] == "design"
        assert role["source"] == "design"
        assert role["voice_id"] == role["role_id"]
        assert role["description"] == ""
    assert casting["voice_library_size"] == 2
    assert casting["narrator_voice"] == "narrator"


def test_build_casting_keeps_a_manual_library_choice(settings):
    """手工绑过库存音色的角色：保留绑定，改成走克隆通道。"""
    previous = {
        "roles": {
            "role_0001": {
                "role_id": "role_0001",
                "voice_id": "v_nar",
                "voice_name": "音色v_nar",
                "source": "manual",
            }
        }
    }
    casting, _ = build_casting(
        _runner(settings, FakeLLM(routes={"【VOICE_RECOMMEND】": _route})),
        book_id="b1",
        characters=CHARACTERS,
        samples=SAMPLES,
        voices=[_voice("v_nar"), _voice("v_hero")],
        previous=previous,
    )
    hero = casting["roles"]["role_0001"]
    assert hero["voice_id"] == "v_nar"
    assert hero["voice_source"] == "library"
    assert hero["source"] == "manual"
    assert voice_for_speaker(casting, "老苏") == "v_nar"


def test_build_casting_keeps_an_existing_description(settings):
    """重跑登记不会把已经写好的音色描述冲掉。"""
    previous = {
        "roles": {
            "role_0001": {
                "role_id": "role_0001",
                "source": "design",
                "description": "二十出头的年轻男性，嗓音偏低。",
                "description_source": "manual",
                "sample": "别废话。",
            }
        }
    }
    casting, _ = build_casting(
        None,
        book_id="b1",
        characters=CHARACTERS,
        samples=SAMPLES,
        voices=[],
        previous=previous,
    )
    hero = casting["roles"]["role_0001"]
    assert hero["description"] == "二十出头的年轻男性，嗓音偏低。"
    assert hero["description_source"] == "manual"
    assert hero["sample"] == "别废话。"


def test_load_voice_library_reads_directory(settings):
    assert load_voice_library(settings) == []
    path = settings.voices_dir / "v_暗夜玫瑰"
    path.mkdir(parents=True, exist_ok=True)
    (path / "voice.json").write_text(
        '{"name":"暗夜玫瑰","gender":"女","age_group":"中年","personality":["魅惑/诱惑"],'
        '"mood":["浪漫"],"speech_rate":"慢","usage_type":["角色对话"],"description":"低沉磁性"}',
        encoding="utf-8",
    )
    voices = load_voice_library(settings)
    assert len(voices) == 1
    assert voices[0].id == "v_暗夜玫瑰"
    assert voices[0].personality == ("魅惑", "诱惑")
    assert voices[0].speech_rate == "慢"


def test_voice_for_speaker_accepts_role_id_name_and_alias():
    casting = {
        "names": {"苏锐": "role_0001", "老苏": "role_0001"},
        "roles": {"role_0001": {"voice_id": "v_hero"}},
    }
    assert voice_for_speaker(casting, "role_0001") == "v_hero"
    assert voice_for_speaker(casting, "苏锐") == "v_hero"
    assert voice_for_speaker(casting, "老苏") == "v_hero"
    assert voice_for_speaker(casting, "不存在") is None
