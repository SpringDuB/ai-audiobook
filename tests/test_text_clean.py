from audiobook.text.clean import clean_text


SAMPLE = """-‍﻿‍​‍‌‍﻿

更多免费网盘资源，浏览器访问 mi.panlay.com‍‍‌﻿‌­­‌﻿‌﻿

------------

序章

    以下是啃书小说网

    "秦风，你为什么要杀我我跟你几年，辛辛苦苦，任劳任怨，没想到到头要死在你手里"
"""


def test_clean_text_drops_noise_lines():
    result = clean_text(SAMPLE)
    assert "panlay" not in result.text
    assert "啃书小说网" not in result.text
    assert "-----------" not in result.text
    assert "秦风，你为什么要杀我" in result.text
    assert result.stats["dropped"] >= 4
    assert any("panlay" in line for line in result.dropped)


def test_clean_text_strips_title_suffix():
    result = clean_text("第三章 洗髓伐脉（新书求收藏）\n\n正文内容。")
    assert result.text.startswith("第三章 洗髓伐脉")
    assert "求收藏" not in result.text


def test_clean_text_strips_html_tags_and_entities():
    """抓取站把 HTML 直接塞进 txt：标签、实体、<br> 都要还原成正文行。"""
    result = clean_text(
        "<p>第一章&nbsp;开始</p><br/>\n"
        "<div>他抬头看了一眼，天已经黑了。</div>\n"
        "&nbsp;\n"
        "她说：&ldquo;走吧。&rdquo;\n"
    )
    assert "<p>" not in result.text and "&nbsp;" not in result.text
    assert "第一章 开始" in result.text
    assert "他抬头看了一眼，天已经黑了。" in result.text
    assert "她说：“走吧。”" in result.text
    assert result.stats["kept"] == 3


def test_clean_text_drops_site_ads_navigation_and_watermarks():
    text = (
        "第一章 风起\n\n"
        "【笔趣阁】请记住本站，最新章节无弹窗！\n\n"
        "他推开门，屋里空无一人。\n\n"
        "更多免费小说资源，请访问 www.example-novel.com\n\n"
        "上一章 返回目录 下一章\n\n"
        "求月票，求订阅！\n\n"
        "正文里带着水印 www.example-novel.com 但这一句是正文。\n"
    )
    result = clean_text(text)
    assert "最新章节" not in result.text
    assert "返回目录" not in result.text
    assert "求月票" not in result.text
    assert "example-novel.com" not in result.text
    assert "他推开门，屋里空无一人。" in result.text
    assert "但这一句是正文。" in result.text
    assert result.stats["dropped"] >= 4


def test_clean_text_drops_duplicated_paragraphs_but_keeps_short_repeats():
    long_line = "苏锐站在门口，看着院子里那棵老槐树，一句话也没说。"
    result = clean_text(f"{long_line}\n{long_line}\n\n哈哈！\n哈哈！\n")
    assert result.text.count(long_line) == 1
    assert result.text.count("哈哈！") == 2


def test_clean_text_keeps_real_prose_that_mentions_banned_words():
    """长行里的"月票/打赏"是剧情就保留：垃圾行判定只对短行生效。"""
    line = "他把那叠月票和打赏的钱一起塞进信封，交给了门口的老张，转身走进雨里。"
    result = clean_text(line)
    assert result.text == line
