"""引语切分与说话人提示：回归"台词和旁白混在一起、被标成旁白"这个老问题。"""

from audiobook.analysis.attribution import hints_for_units
from audiobook.analysis.chapter import materialize
from audiobook.analysis.models import LineAnnotation
from audiobook.text.dialogue import split_units

CHARACTERS = {
    "characters": [
        {
            "id": "narrator", "name": "旁白", "aliases": [], "gender": "未知", "age_group": "未知",
            "speaking_style": "平稳", "base_emotion": "平静", "is_narrator": True,
        },
        {
            "id": "role_0001", "name": "张卫东", "aliases": ["卫东"], "gender": "男", "age_group": "青年",
            "speaking_style": "沉稳", "base_emotion": "平静",
        },
        {
            "id": "role_0002", "name": "王胖子", "aliases": ["王胜"], "gender": "男", "age_group": "青年",
            "speaking_style": "咋呼", "base_emotion": "喜悦",
        },
    ],
    "relationships": [],
}


def _kinds(units):
    return [(unit.kind, unit.text) for unit in units]


def test_dialogue_after_quote_is_split_from_speech():
    """「“台词”X 一听，吃惊道。」必须拆成台词 + 旁白，这是用户报的那条。"""
    text = "“不是吧这荣镇鸟不拉屎的小地方，你打算干啥咱还年轻，不会就这么准备养老吧”王胖子一听，吃惊道。"
    assert _kinds(split_units(text)) == [
        ("dialogue", "不是吧这荣镇鸟不拉屎的小地方，你打算干啥咱还年轻，不会就这么准备养老吧"),
        ("narration", "王胖子一听，吃惊道。"),
    ]


def test_two_quotes_in_one_paragraph_are_two_dialogue_units():
    text = (
        "“狗p，我就一小科员，舅舅不亲，姥姥不爱的，这次还是拍了领导马屁，才跟来的，卫东，哥们难啊”"
        "王胖子一屁股坐下，倒一肚子苦水。"
        "“倒是你，不声不响就闪人，忒不够意思，我以为你失踪了，打算报警呢”"
    )
    assert _kinds(split_units(text)) == [
        ("dialogue", "狗p，我就一小科员，舅舅不亲，姥姥不爱的，这次还是拍了领导马屁，才跟来的，卫东，哥们难啊"),
        ("narration", "王胖子一屁股坐下，倒一肚子苦水。"),
        ("dialogue", "倒是你，不声不响就闪人，忒不够意思，我以为你失踪了，打算报警呢"),
    ]


def test_leading_attribution_stays_narration():
    units = split_units("张卫东歉意的笑道：“这次是我不对，有点急事，去了趟云南”")
    assert _kinds(units) == [
        ("narration", "张卫东歉意的笑道："),
        ("dialogue", "这次是我不对，有点急事，去了趟云南"),
    ]


def test_short_reply_and_pause_only_quote_count_as_speech():
    assert _kinds(split_units("“嗯。”他点点头。")) == [("dialogue", "嗯。"), ("narration", "他点点头。")]
    assert _kinds(split_units("“……”他沉默了。")) == [("dialogue", "……"), ("narration", "他沉默了。")]


def test_emphasis_quotes_stay_in_narration():
    """绰号、强调用的引号不是台词，不能切出来当对白。"""
    text = "他被称为“天才”，其实并不聪明。所谓“一号工程”，不过是个幌子。"
    units = split_units(text)
    assert all(unit.kind == "narration" for unit in units)
    assert "".join(unit.text for unit in units) == text


def test_hints_come_from_quote_neighbours():
    units = split_units(
        "张卫东歉意的笑道：“这次是我不对。”\n“不是吧，你打算干啥？”王胖子一听，吃惊道。\n“我打算开个铺子。”"
    )
    hints = hints_for_units(units, CHARACTERS)
    assert [unit.kind for unit in units] == ["narration", "dialogue", "dialogue", "narration", "dialogue"]
    assert hints == [None, "张卫东", "王胖子", None, None]


def test_dialogue_marked_narrator_is_corrected_by_hint():
    """模型把带归属句的对白标成旁白——按归属句纠正，而不是照单全收。"""
    units = split_units("张卫东歉意的笑道：“这次是我不对。”\n“不是吧，你打算干啥？”王胖子一听，吃惊道。")
    annotations = [
        LineAnnotation(index=index, speaker="旁白", emotion="平静", intensity=0.4)
        for index in range(1, len(units) + 1)
    ]
    lines, _ = materialize(
        chapter_index=1,
        units=units,
        annotations=annotations,
        characters_payload=CHARACTERS,
        pronounce_table={},
    )
    assert [line["kind"] for line in lines] == ["narration", "dialogue", "dialogue", "narration"]
    assert [line["speaker"] for line in lines] == ["narrator", "role_0001", "role_0002", "narrator"]
    # 引号已经从台词里去掉，旁白保留归属句
    assert lines[1]["text"] == "这次是我不对。"
    assert lines[3]["text"] == "王胖子一听，吃惊道。"
