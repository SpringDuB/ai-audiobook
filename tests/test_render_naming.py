from audiobook import store
from audiobook.render.naming import book_title, chapter_label, chapter_title, safe_filename


def _chapters(settings, rows):
    store.atomic_replace_json(store.chapters_path(settings, "b1"), {"chapters": rows})


def test_chapter_label_keeps_existing_chinese_prefix(settings):
    _chapters(
        settings,
        [
            {"index": 1, "title": "第一章 重生十年前"},
            {"index": 2, "title": "死党王胖子"},
            {"index": 3, "title": "第3章 洗髓伐脉"},
            {"index": 4, "title": ""},
        ],
    )
    assert chapter_label(settings, "b1", 1) == "第一章 重生十年前"
    assert chapter_label(settings, "b1", 2) == "第2章 死党王胖子"
    assert chapter_label(settings, "b1", 3) == "第3章 洗髓伐脉"
    assert chapter_label(settings, "b1", 4) == "第4章"
    assert chapter_label(settings, "b1", 9) == "第9章"          # chapters.json 里没有


def test_chapter_title_and_book_title(settings):
    _chapters(settings, [{"index": 0, "title": "卷一"}])
    store.atomic_replace_json(store.book_dir(settings, "b1") / "book.json", {"title": "地球最后一个修仙者"})
    assert chapter_title(settings, "b1", 0) == "卷一"
    assert book_title(settings, "b1") == "地球最后一个修仙者"
    assert book_title(settings, "missing") == "book"
    assert chapter_label(settings, "b1", 0) == "第0章 卷一"


def test_safe_filename_strips_unsafe_characters():
    assert safe_filename('卷一/起风: "重生"') == "卷一_起风_ _重生_"
    assert len(safe_filename("长" * 100)) == 60
    assert safe_filename("") == ""
