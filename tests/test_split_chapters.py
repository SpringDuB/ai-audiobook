from audiobook.text.split_chapters import split_chapters, split_sentences


def test_split_chapters_builds_preface_and_chapters():
    text = "简介：这是一本书\n\n第一章 开始\n\n正文一。\n\n第二章 继续\n\n正文二。"
    chapters = split_chapters(text)
    assert [c.title for c in chapters] == ["前言", "第一章 开始", "第二章 继续"]
    assert [c.index for c in chapters] == [0, 1, 2]
    assert "正文一。" in chapters[1].content
    assert chapters[2].content.strip() == "正文二。"


def test_split_sentences_splits_on_cjk_punctuation():
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    assert split_sentences("只有一句没有句号") == ["只有一句没有句号"]


def test_split_sentences_merges_orphan_closing_quotes():
    assert split_sentences("苏锐说：“走。”") == ["苏锐说：“走。”"]
    assert split_sentences("“好。”他说。") == ["“好。”", "他说。"]
    assert split_sentences("……") == ["……"]
