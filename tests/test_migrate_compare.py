from audiobook.migrate.compare import compare_chapters, compare_roles


def test_compare_chapters_aligns_by_title_first():
    old = [
        {"index": 0, "title": "前言", "content": "x" * 10},
        {"index": 1, "title": "卷一", "content": "y" * 100},
    ]
    new = [{"index": 0, "title": "卷一", "content": "y" * 90}]
    result = compare_chapters(old, new)
    assert (result["old_count"], result["new_count"]) == (2, 1)
    matched = [row for row in result["rows"] if row["title_match"]]
    assert matched[0]["aligned_by"] == "title"
    assert matched[0]["chars_delta"] == -10
    assert matched[0]["old_index"] == 1 and matched[0]["new_index"] == 0
    assert result["old_only"] == ["前言"]
    assert result["new_only"] == []


def test_compare_chapters_all_equal():
    rows = [{"index": 0, "title": "卷一", "content": "abc"}]
    result = compare_chapters(rows, rows)
    assert result["title_match_count"] == 1 and result["chars_delta_total"] == 0


def test_compare_chapters_matches_titles_with_bracket_suffixes():
    old = [
        {"index": 0, "title": "第三章 洗髓伐脉（新书求收藏）", "content": "a" * 10},
        {"index": 1, "title": "第六章 种菜（求收藏推荐包养）", "content": "b" * 10},
    ]
    new = [
        {"index": 0, "title": "第三章 洗髓伐脉", "content": "a" * 8},
        {"index": 1, "title": "第六章 种菜", "content": "b" * 9},
    ]
    result = compare_chapters(old, new)
    assert result["title_match_count"] == 2
    assert [row["aligned_by"] for row in result["rows"]] == ["title_loose", "title_loose"]
    assert result["old_only"] == [] and result["new_only"] == []


def test_compare_roles_matches_sentences_and_counts_agreement():
    old = [
        [
            {"text": "秦风，你为什么要杀我", "role": "张卫东"},
            {"text": "燕京的郊外。", "role": "旁白"},
        ]
    ]
    new = {
        0: [
            {"text": "秦风，你为什么要杀我。", "speaker_name": "张卫东"},
            {"text": "燕京的郊外。", "speaker_name": "旁白"},
            {"text": "别的句子。", "speaker_name": "旁白"},
        ]
    }
    result = compare_roles(old, new)
    assert (result["old_lines"], result["matched"], result["agree"]) == (2, 2, 2)
    assert result["agreement_rate"] == 1.0
    assert result["mismatches"] == []


def test_compare_roles_lists_mismatches():
    old = [[{"text": "你是谁？", "role": "张卫东"}]]
    new = {0: [{"text": "你是谁？", "speaker_name": "旁白"}]}
    result = compare_roles(old, new)
    assert (result["matched"], result["agree"]) == (1, 0)
    assert result["agreement_rate"] == 0.0
    assert result["mismatches"][0] == {"text": "你是谁？", "legacy": "张卫东", "new": "旁白"}
    assert result["mismatch_total"] == 1
    assert result["new_narrator_fallback"] == 1
    assert result["legacy_narrator_reassigned"] == 0


def test_compare_roles_counts_narrator_reassignment():
    """旧系统含糊标成旁白、新系统认出说话人 —— 这类差异要单独计数。"""
    old = [[{"text": "小张起来了，吃点什么。", "role": "旁白"}]]
    new = {0: [{"text": "小张起来了，吃点什么。", "speaker_name": "老刘"}]}
    result = compare_roles(old, new)
    assert (result["agree"], result["legacy_narrator_reassigned"]) == (0, 1)
    assert result["new_narrator_fallback"] == 0


def test_compare_roles_ignores_unmatched_old_sentences():
    old = [[{"text": "旧系统有但新系统没有的句子", "role": "旁白"}]]
    result = compare_roles(old, {0: [{"text": "新句子。", "speaker_name": "旁白"}]})
    assert (result["old_lines"], result["matched"]) == (1, 0)
    assert result["agreement_rate"] is None
