from audiobook.analysis.casting import (
    VoiceProfile,
    build_casting,
    character_rate,
    load_voice_library,
    score_voice,
    voice_for_speaker,
)


def _voice(voice_id: str, **overrides) -> VoiceProfile:
    base = dict(
        id=voice_id, name=voice_id, gender="男", age_group="青年", personality=(), genres=(),
        mood=(), speech_rate="中", voice_quality=(), language_style=("普通话",), usage_type=("角色对话",),
        description="",
    )
    base.update(overrides)
    return VoiceProfile(**base)


def _character(**overrides) -> dict:
    base = {
        "id": "role_0001", "name": "秦风", "aliases": [], "gender": "男", "age_group": "青年",
        "personality": ["冷酷", "沉稳"], "speaking_style": "语速偏慢", "base_emotion": "平静",
        "base_intensity": 0.4, "chapters": [1, 2], "mentions": 4, "is_narrator": False,
    }
    base.update(overrides)
    return base


def _narrator() -> dict:
    return {
        "id": "narrator", "name": "旁白", "aliases": [], "gender": "未知", "age_group": "未知",
        "personality": [], "speaking_style": "平稳", "base_emotion": "平静", "base_intensity": 0.3,
        "chapters": [1], "mentions": 1, "is_narrator": True,
    }


def test_character_rate_maps_speaking_style():
    assert character_rate({"speaking_style": "说话很快"}) == "快"
    assert character_rate({"speaking_style": "语速偏慢"}) == "慢"
    assert character_rate({"speaking_style": ""}) == "中"


def test_gender_mismatch_disqualifies():
    score, reasons, qualified = score_voice(_character(), _voice("v_f", gender="女"))
    assert qualified is False and score == 0.0
    assert "性别不符" in reasons[0]


def test_full_score_is_sum_of_weighted_items():
    voice = _voice(
        "v_1", age_group="青年", speech_rate="慢", personality=("冷酷", "沉稳"),
        mood=("冷酷",), voice_quality=("磁性",), usage_type=("角色对话",), description="冷酷而沉稳的男声",
    )
    score, reasons, qualified = score_voice(_character(), voice)
    # 年龄 30 + 语速 20 + 性格 16（2×8）+ 基调 6 + 用途 6 + 描述 6（2×3）= 84
    # voice_quality 是"磁性"，与角色性格词不重合，所以不加分
    assert qualified is True
    assert score == 84.0
    assert "年龄段一致 +30" in reasons
    assert "语速一致 +20" in reasons


def test_voice_quality_and_book_genres_add_points():
    voice = _voice(
        "v_1", personality=("冷酷", "沉稳"), voice_quality=("沉稳",),
        genres=("都市", "言情"), speech_rate="慢",
    )
    score, reasons, _ = score_voice(_character(), voice, book_genres=("都市", "古风"))
    # 年龄 30 + 语速 20 + 性格 16 + 质地 5（沉稳）+ 题材 5（都市）+ 用途 6 = 82
    assert score == 82.0
    assert "音色匹配 +5" in reasons
    assert "题材先验 +5" in reasons


def test_narrator_prefers_narration_voices():
    narrator_voice = _voice(
        "v_nar", gender="女", age_group="中年", usage_type=("旁白叙述",), description="沉稳旁白", mood=("平稳",)
    )
    dialogue_voice = _voice("v_dia", usage_type=("角色对话",))
    score, reasons, _ = score_voice(_narrator(), narrator_voice, is_narrator=True)
    assert score > 0 and "旁白叙述用途 +12" in reasons
    assert score_voice(_narrator(), dialogue_voice, is_narrator=True)[0] < score


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


def test_build_casting_assigns_narrator_first_and_avoids_reuse():
    characters = {
        "characters": [
            _narrator(),
            _character(),
            _character(id="role_0002", name="王胖子", chapters=[1], personality=[]),
        ]
    }
    voices = [
        _voice("v_nar", gender="女", usage_type=("旁白叙述",)),
        _voice("v_hero", personality=("冷酷", "沉稳"), speech_rate="慢"),
        _voice("v_buddy", personality=(), speech_rate="中"),
    ]
    casting, issues = build_casting(characters, voices, book_id="b1")
    assert casting["roles"]["narrator"]["voice_id"] == "v_nar"
    assert casting["roles"]["role_0001"]["voice_id"] == "v_hero"
    assert casting["roles"]["role_0002"]["voice_id"] == "v_buddy"
    assert casting["narrator_voice"] == "v_nar"
    assert issues == []
    assert casting["names"]["秦风"] == "role_0001"
    assert voice_for_speaker(casting, "秦风") == "v_hero"
    assert voice_for_speaker(casting, "role_0002") == "v_buddy"
    assert voice_for_speaker(casting, "不存在") is None


def test_build_casting_records_reuse_when_pool_is_exhausted():
    characters = {
        "characters": [_narrator(), _character(), _character(id="role_0002", name="王胖子", chapters=[1])]
    }
    voices = [_voice("v_nar", gender="女", usage_type=("旁白叙述",)), _voice("v_only")]
    casting, issues = build_casting(characters, voices, book_id="b1")
    assert casting["roles"]["role_0001"]["voice_id"] == "v_only"
    assert casting["roles"]["role_0002"]["voice_id"] == "v_only"
    assert [issue["kind"] for issue in issues] == ["casting_voice_reused"]
    assert issues[0]["detail"]["shared_with"] == "role_0001"


def test_build_casting_without_voices_falls_back_to_default():
    casting, issues = build_casting({"characters": [_narrator(), _character()]}, [], book_id="b1")
    assert casting["voice_library_size"] == 0
    assert {role["voice_id"] for role in casting["roles"].values()} == {"default"}
    assert [issue["kind"] for issue in issues] == ["voice_library_empty"]
    assert voice_for_speaker(casting, "秦风") == "default"


def test_build_casting_relaxes_gender_when_nothing_matches():
    characters = {"characters": [_narrator(), _character()]}
    voices = [_voice("v_nar", gender="女", usage_type=("旁白叙述",)), _voice("v_f", gender="女")]
    casting, issues = build_casting(characters, voices, book_id="b1")
    assert casting["roles"]["role_0001"]["voice_id"] == "v_f"
    assert [issue["kind"] for issue in issues] == ["casting_no_match"]
    assert "放宽性别约束" in issues[0]["reason"]
