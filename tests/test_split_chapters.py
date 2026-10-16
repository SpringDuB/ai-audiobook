from audiobook.text.split_chapters import split_chapters, split_sentences


def test_split_chapters_builds_preface_and_chapters():
    text = "简介：这是一本书\n\n第一章 开始\n\n正文一。\n\n第二章 继续\n\n正文二。"
    chapters = split_chapters(text)
    assert [c.title for c in chapters] == ["前言", "第一章 开始", "第二章 继续"]
    assert [c.index for c in chapters] == [0, 1, 2]
    assert "正文一。" in chapters[1].content
    assert chapters[2].content.strip() == "正文二。"


def test_split_chapters_recognizes_extra_sections():
    text = (
        "第1章 正文\n\n正文一。\n\n番外：浴房篇1\n\n番外一正文。\n\n"
        "番外篇2 标题\n\n番外二正文。\n\n番外3\n\n番外三正文。\n\n番外 附加\n\n番外四正文。"
    )
    chapters = split_chapters(text)
    assert [c.title for c in chapters] == ["第1章 正文", "番外：浴房篇1", "番外篇2 标题", "番外3", "番外 附加"]
    assert chapters[1].content.strip() == "番外一正文。"
    assert chapters[3].content.strip() == "番外三正文。"


def test_split_chapters_keeps_body_lines_starting_with_extra_words():
    # 正文叙述行（含句读、或以"的"接续）不能误判成标题
    text = "第1章 正文\n\n番外的话题说到这里。\n\n番外一的事情早就结束了。\n\n后面的正文。"
    chapters = split_chapters(text)
    assert [c.title for c in chapters] == ["第1章 正文"]
    assert "番外的话题说到这里。" in chapters[0].content
    assert "番外一的事情早就结束了。" in chapters[0].content
    assert "后面的正文。" in chapters[0].content


def test_split_sentences_splits_on_cjk_punctuation():
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    assert split_sentences("只有一句没有句号") == ["只有一句没有句号"]


def test_split_sentences_merges_orphan_closing_quotes():
    assert split_sentences("苏锐说：“走。”") == ["苏锐说：“走。”"]
    assert split_sentences("“好。”他说。") == ["“好。”", "他说。"]
    assert split_sentences("……") == ["……"]
