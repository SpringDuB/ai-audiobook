"""P0：选角表（角色互不撞车）、样本跨章抽样 + 身份线索、基础描述职责切开、微调锚点。

都是纯函数/纯文本层面的行为，不碰网络与 TTS。
"""

from audiobook.analysis.design import (
    build_cast_sheet,
    cast_sheet_participants,
    cast_sheet_rows,
    describe_role,
    role_briefs,
    top_avoid_rows,
)
from audiobook.analysis.models import CastSheetOutput
from audiobook.analysis.prompts import cast_sheet_user, voice_design_user
from audiobook.analysis.voices import compose_instruct
from audiobook.llm.runner import LlmJsonError

CHARACTERS = {
    "characters": [
        {"id": "narrator", "name": "旁白", "aliases": [], "is_narrator": True},
        {"id": "role_0001", "name": "苏锐", "aliases": ["小苏"]},
        {"id": "role_0002", "name": "林可", "aliases": []},
    ]
}


def _row(speaker, text, kind="dialogue"):
    return {"speaker": speaker, "text": text, "kind": kind}


class _Runner:
    """最小 runner 替身：直接把预设的模型对象/异常发回来。"""

    def __init__(self, output=None, error: Exception | None = None, errors: dict[int, Exception] | None = None):
        self.output = output
        self.error = error
        self.errors = dict(errors or {})
        self.calls: list[dict] = []

    def run(self, *, system, user, model_cls, pass_name, book_id, cancel_check=None, **kwargs):
        self.calls.append({"user": user, "pass": pass_name, "model_cls": model_cls})
        boom = self.errors.get(len(self.calls))
        if boom is not None:
            raise boom
        if self.error is not None:
            raise self.error
        data = self.output(user) if callable(self.output) else self.output
        if isinstance(data, model_cls):
            return data
        return model_cls.model_validate(data)


# ---------------------------------------------------------------- 样本抽样

def test_samples_spread_across_chapters_and_skip_noise():
    lines = {
        1: [_row("role_0001", "嗯。"), _row("role_0001", "你是谁？")],
        2: [_row("role_0001", "我叫苏锐，是这所学校的老师。")],
        3: [_row("role_0001", "别慌，我带你出去。")],
    }
    briefs = role_briefs(CHARACTERS, lines)
    samples = briefs["role_0001"]["samples"]

    # 逐章轮转：每个出现过的章节都先出一句，语气词样本排在最后（样本够时根本不进池）
    assert "我叫苏锐，是这所学校的老师。" in samples
    assert "别慌，我带你出去。" in samples
    assert samples.index("我叫苏锐，是这所学校的老师。") < len(samples)
    assert "嗯。" not in samples


def test_narration_clues_come_from_narration_lines():
    lines = {
        1: [
            _row("narrator", "苏锐站在院子里，二十出头的年纪。", "narration"),
            _row("role_0001", "谁？"),
        ],
        2: [
            _row("narrator", "小苏是村里唯一读过书的人。", "narration"),
            _row("role_0001", "我知道。"),
        ],
    }
    briefs = role_briefs(CHARACTERS, lines)
    clues = briefs["role_0001"]["clues"]

    assert any("苏锐站在院子里" in clue for clue in clues)
    assert any("小苏" in clue for clue in clues)  # 别名也算
    # 旁白自己不该把"提到自己的句子"当身份线索
    assert briefs["narrator"]["clues"] == []


def test_narrator_keeps_narration_as_samples():
    lines = {1: [_row("narrator", "夜色渐深，故事从这里开始。", "narration")]}
    briefs = role_briefs(CHARACTERS, lines)
    assert briefs["narrator"]["samples"] == ["夜色渐深，故事从这里开始。"]
    assert briefs["narrator"]["is_narrator"] is True


# ---------------------------------------------------------------- 选角表

def test_cast_sheet_rows_mark_existing_description():
    briefs = role_briefs(CHARACTERS, {1: [_row("role_0001", "我是苏锐。")]})
    rows = cast_sheet_rows([briefs["role_0001"]], known={"role_0001": {"description": "中低音区，厚实"}})
    assert rows[0]["note"] == "已定音色：中低音区，厚实"
    assert "已定音色" in cast_sheet_user(rows)


def test_build_cast_sheet_maps_names_and_reports_missing():
    briefs = [{"role_id": "role_0001", "name": "苏锐", "lines": 30}, {"role_id": "role_0002", "name": "林可", "lines": 20}]
    runner = _Runner(output={"characters": [{"name": "苏锐", "archetype": "中高音区，清亮"}]})
    archetypes, issues = build_cast_sheet(runner, book_id="b1", briefs=briefs)

    assert archetypes == {"苏锐": "中高音区，清亮"}
    assert [issue["kind"] for issue in issues] == ["cast_sheet_failed"]
    assert "林可" in issues[0]["reason"]
    assert runner.calls[0]["pass"] == "cast_sheet"


def test_build_cast_sheet_failure_falls_back_to_per_role_design():
    briefs = [{"role_id": "role_0001", "name": "苏锐", "lines": 30}, {"role_id": "role_0002", "name": "林可", "lines": 20}]
    runner = _Runner(error=LlmJsonError("模型没吐 JSON", attempts=2, last_error="Expecting value"))
    archetypes, issues = build_cast_sheet(runner, book_id="b1", briefs=briefs)

    assert archetypes == {}
    assert [issue["kind"] for issue in issues] == ["cast_sheet_failed"]


def test_build_cast_sheet_skips_single_role_book():
    runner = _Runner(output={"characters": []})
    archetypes, issues = build_cast_sheet(runner, book_id="b1", briefs=[{"role_id": "narrator", "name": "独白", "lines": 9}])
    assert (archetypes, issues) == ({}, [])
    assert runner.calls == []


def test_cast_sheet_participants_skip_rare_roles_and_cap_the_list():
    """600+ 角色的书：只有有戏份的角色参与，且按台词数封顶 —— 不能一次全交给模型。"""
    briefs = [
        {"role_id": f"role_{index:04d}", "name": f"角色{index}", "lines": index}
        for index in range(1, 201)
    ]
    picked = cast_sheet_participants(briefs, min_lines=4, max_roles=50)

    assert len(picked) == 50
    assert [item["name"] for item in picked[:3]] == ["角色200", "角色199", "角色198"]
    assert all(int(item["lines"]) >= 4 for item in picked)
    assert "角色3" not in {item["name"] for item in picked}


def test_cast_sheet_participants_always_include_narrator():
    """旁白是全书出现最多的声音，哪怕这一版数据里句数很少也要参与（别人得绕开它）。"""
    briefs = [
        {"role_id": "narrator", "name": "旁白", "lines": 2, "is_narrator": True},
        {"role_id": "role_0001", "name": "苏锐", "lines": 40},
        {"role_id": "role_0002", "name": "龙套甲", "lines": 1},
    ]
    picked = cast_sheet_participants(briefs, min_lines=4, max_roles=10)
    assert [item["name"] for item in picked] == ["旁白", "苏锐"]


def _echo_archetypes(names):
    def route(user: str) -> dict:
        return {
            "characters": [{"name": name, "archetype": f"{name}的原型"} for name in names if name in user]
        }

    return route


def test_cast_sheet_batches_carry_avoid_list_across_calls():
    briefs = [
        {"role_id": f"r{index}", "name": name, "lines": 100 - index}
        for index, name in enumerate(["苏锐", "林可", "王胖子", "龙二", "家珍"])
    ]
    runner = _Runner(output=_echo_archetypes(["苏锐", "林可", "王胖子", "龙二", "家珍"]))
    archetypes, issues = build_cast_sheet(runner, book_id="b1", briefs=briefs, batch_size=2)

    assert len(runner.calls) == 3  # 5 个角色、每批 2 个
    assert "苏锐的原型" not in runner.calls[0]["user"]  # 第一批没有已占用
    assert "苏锐的原型" in runner.calls[1]["user"]  # 第二批看得到第一批定的
    assert "王胖子的原型" in runner.calls[2]["user"]
    assert set(archetypes) == {"苏锐", "林可", "王胖子", "龙二", "家珍"}
    assert issues == []


def test_cast_sheet_continues_after_one_batch_fails():
    briefs = [
        {"role_id": f"r{index}", "name": name, "lines": 50 - index}
        for index, name in enumerate(["苏锐", "林可", "王胖子", "龙二"])
    ]
    runner = _Runner(
        output=_echo_archetypes(["苏锐", "林可", "王胖子", "龙二"]),
        errors={1: LlmJsonError("第一批发疯", attempts=2, last_error="boom")},
    )
    archetypes, issues = build_cast_sheet(runner, book_id="b1", briefs=briefs, batch_size=2)

    assert set(archetypes) == {"王胖子", "龙二"}  # 第一批失败，第二批照跑
    assert [issue["kind"] for issue in issues] == ["cast_sheet_failed"]
    assert "第 1/2 批失败" in issues[0]["reason"]


def test_top_avoid_rows_returns_frequent_roles_only():
    archetypes = {"苏锐": "中高音区清亮", "林可": "高音区甜美"}
    briefs = [
        {"role_id": "r1", "name": "苏锐", "lines": 200},
        {"role_id": "r2", "name": "林可", "lines": 100},
        {"role_id": "r3", "name": "龙套甲", "lines": 1},
    ]
    rows = top_avoid_rows(archetypes, briefs)
    assert [row["name"] for row in rows] == ["苏锐", "林可"]


# ---------------------------------------------------------------- 提示词与拼装

def test_voice_design_user_carries_archetype_clues_and_previous():
    text = voice_design_user(
        "苏锐",
        ["小苏"],
        ["我知道。"],
        3,
        clues=["苏锐站在院子里，二十出头的年纪。"],
        archetype="二十出头男性，中高音区，清亮",
        previous="二十出头的年轻男性，嗓音偏低。",
    )

    assert "中高音区，清亮" in text
    assert "苏锐站在院子里" in text
    assert "上一版描述" in text
    assert "微调" in text


def test_voice_design_prompt_forbids_speed_and_emotion_in_base_description():
    text = voice_design_user("苏锐", [], ["我知道。"], 3)
    # 语速/情绪归逐句描述，基础描述里必须明确禁止
    assert "【不要写】语速、音量、气息、情绪、语气" in text
    assert "音区" in text and "音色质地" in text


def test_voice_design_user_renders_user_directive_and_avoid_list():
    text = voice_design_user(
        "苏锐",
        [],
        ["我知道。"],
        30,
        directive="换成四十岁左右的中年男声，音区更低、嗓音沙哑些",
        avoid=[
            {"name": "苏锐", "archetype": "这个角色自己的原型不该出现"},
            {"name": "旁白", "archetype": "中低音区，厚实"},
        ],
    )

    assert "【用户要求（硬性）】" in text
    assert "换成四十岁左右的中年男声" in text
    assert "【已占用音色（别和这些角色撞）】" in text
    assert "中低音区，厚实" in text
    assert "这个角色自己的原型不该出现" not in text  # 避让清单里不能出现这个角色自己


def test_voice_design_user_without_directive_has_no_user_section():
    text = voice_design_user("苏锐", [], ["我知道。"], 30)
    assert "【用户要求（硬性）】" not in text
    assert "【已占用音色" not in text


def test_describe_role_passes_previous_only_when_given():
    brief = role_briefs(CHARACTERS, {1: [_row("role_0001", "我是苏锐。")]})["role_0001"]
    plain = _Runner(output={"description": "中高音区，清亮。", "sample": "我是苏锐。"})
    describe_role(plain, book_id="b1", brief=brief)
    assert "上一版描述" not in plain.calls[0]["user"]

    refined = _Runner(output={"description": "中高音区，清亮。", "sample": "我是苏锐。"})
    describe_role(refined, book_id="b1", brief=brief, previous="中低音区，厚实。")
    assert "上一版描述：中低音区，厚实。" in refined.calls[0]["user"]


def test_cast_sheet_output_tolerates_wrapper_and_extra_fields():
    parsed = CastSheetOutput.model_validate(
        {"characters": [{"name": "苏锐", "archetype": "中高音区", "note": "多余字段"}]}
    )
    assert parsed.characters[0].archetype == "中高音区"


def test_compose_instruct_keeps_identity_and_line_prompt_apart():
    text = compose_instruct("中高音区，音色清亮", "压低声音，语速放慢")
    # 拼接格式进合成缓存键，必须保持稳定（改了就等于全书音频作废重跑）
    assert text == "中高音区，音色清亮。这一句：压低声音，语速放慢。"
    # 只有基础描述时不要凭空加语气段
    assert compose_instruct("中高音区，音色清亮", "") == "中高音区，音色清亮。"
    # 只有逐句描述时兜底基础描述仍在
    assert compose_instruct("", "不耐烦地提高音量").startswith("自然清晰的声音")


# ---------------------------------------------------------------- handler 接线

def _seed_design_book(settings) -> None:
    from audiobook import store

    store.atomic_replace_json(store.characters_path(settings, "book1"), CHARACTERS)
    store.atomic_replace_json(
        store.chapters_path(settings, "book1"),
        {"chapters": [{"index": 1, "title": "第一章", "content": "别废话。", "chars": 4}]},
    )
    store.write_jsonl_atomic(
        store.lines_path(settings, "book1", 1),
        [
            {"id": "l1", "kind": "narration", "speaker": "narrator", "text": "夜色很深。"},
            {"id": "l2", "kind": "dialogue", "speaker": "role_0001", "text": "别废话，我知道路。"},
            {"id": "l3", "kind": "dialogue", "speaker": "role_0001", "text": "跟着我走，别掉队。"},
            {"id": "l4", "kind": "dialogue", "speaker": "role_0001", "text": "前面就是渡口了。"},
            {"id": "l5", "kind": "dialogue", "speaker": "role_0001", "text": "船家，麻烦渡一趟。"},
        ],
    )
    store.atomic_replace_json(
        store.casting_path(settings, "book1"),
        {
            "book_id": "book1",
            "roles": {
                "role_0001": {
                    "role_id": "role_0001",
                    "name": "苏锐",
                    "source": "design",
                    "voice_source": "design",
                    "description": "二十出头的年轻男性，嗓音偏低，语速不快。",
                    "description_source": "llm",
                }
            },
        },
    )


def _run_voice_design(settings, conn, payload: dict):
    from audiobook import jobs, store
    from audiobook.handlers import voice_design as handler  # noqa: F401  注册 handler
    from audiobook.llm.fake import FakeLLM
    from audiobook.llm.limiter import AdaptiveLimiter
    from audiobook.llm.runner import LlmJsonRunner
    from audiobook.worker import WorkerContext, run_once

    llm = FakeLLM(
        routes={
            "【CAST_SHEET】": lambda _user: {
                "characters": [
                    {"name": "旁白", "archetype": "三十多岁男性，中低音区，嗓音厚实"},
                    {"name": "苏锐", "archetype": "二十出头男性，中高音区，音色清亮"},
                ]
            },
            "【VOICE_DESIGN】": {"description": "二十出头男性，中高音区，音色清亮。", "sample": "我知道路。"},
        }
    )
    ctx = WorkerContext(
        settings=settings,
        conn=conn,
        worker_id="w1",
        llm=LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=2), settings),
    )
    jobs.enqueue(conn, "voice_design", "book1", payload=payload)
    assert run_once(ctx) is True
    role = store.read_json(store.casting_path(settings, "book1"))["roles"]["role_0001"]
    return role, llm


def test_handler_uses_cast_sheet_archetype(settings, conn):
    _seed_design_book(settings)
    role, llm = _run_voice_design(settings, conn, {"roles": ["role_0001"], "force": True, "mode": "reroll"})

    assert role["archetype"] == "二十出头男性，中高音区，音色清亮"
    assert role["description"].startswith("二十出头男性")
    prompts = [call["user"] for call in llm.calls]
    design = next(text for text in prompts if "【VOICE_DESIGN】" in text)
    assert "二十出头男性，中高音区，音色清亮" in design  # 原型进了单角色提示词
    assert "上一版描述" not in design  # reroll：不带锚点


def test_handler_refine_keeps_previous_description_as_anchor(settings, conn):
    _seed_design_book(settings)
    _, llm = _run_voice_design(settings, conn, {"roles": ["role_0001"], "force": True, "mode": "refine"})

    design = next(text for text in (call["user"] for call in llm.calls) if "【VOICE_DESIGN】" in text)
    assert "上一版描述：二十出头的年轻男性，嗓音偏低，语速不快。" in design
    assert "微调" in design


def test_handler_reroll_with_user_instruction(settings, conn):
    """「换一版音色」带用户指令：指令必须进提示词（硬性），并进任务 payload。"""
    _seed_design_book(settings)
    role, llm = _run_voice_design(
        settings,
        conn,
        {
            "roles": ["role_0001"],
            "force": True,
            "mode": "reroll",
            "instruction": "换成五十岁上下的老者，音区更低，嗓音干涩",
        },
    )

    assert role["description"]  # 写进去了
    design = next(text for text in (call["user"] for call in llm.calls) if "【VOICE_DESIGN】" in text)
    assert "【用户要求（硬性）】" in design
    assert "换成五十岁上下的老者" in design
    assert "上一版描述" not in design  # reroll 不带锚点
