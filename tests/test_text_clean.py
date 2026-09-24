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
