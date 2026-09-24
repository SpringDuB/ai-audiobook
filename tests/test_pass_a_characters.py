from audiobook.analysis.characters import chunk_text, extract_chapter, merge_pass_a
from audiobook.analysis.models import PassAOutput
from audiobook.llm.fake import FakeLLM
from audiobook.llm.limiter import AdaptiveLimiter
from audiobook.llm.runner import LlmJsonRunner

PASS_A_JSON = {
    "characters": [
        {"name": "张卫东", "aliases": [], "gender": "男", "age_group": "中年"},
        {"name": "秦风", "aliases": ["秦少"], "gender": "男", "age_group": "青年"},
        {"name": "旁白", "gender": "未知", "age_group": "未知"},
    ],
    "relationships": [
        {"from": "张卫东", "to": "秦风", "closeness": 0.6, "hierarchy": 0.8, "hostility": 0.7, "intimacy": 0.1}
    ],
}


def _runner(settings, llm) -> LlmJsonRunner:
    return LlmJsonRunner(llm, AdaptiveLimiter(max_concurrency=4), settings)


def test_chunk_text_keeps_each_chunk_within_limit_and_loses_nothing():
    text = "第一句。第二句。第三句。第四句。"
    chunks = chunk_text(text, max_chars=10)
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks) == text


def test_extract_chapter_sends_pass_a_prompt_and_parses(settings):
    llm = FakeLLM(routes={"PASS_A": PASS_A_JSON})
    out = extract_chapter(
        _runner(settings, llm), settings=settings, book_id="b1",
        chapter_index=1, title="第一章 重生十年前", content="正文。",
    )
    assert [c.name for c in out.characters] == ["张卫东", "秦风", "旁白"]
    assert out.relationships[0].source == "张卫东"
    assert llm.calls[0]["user"].startswith("【PASS_A】")
    assert "第一章 重生十年前" in llm.calls[0]["user"]


def test_extract_chapter_splits_long_chapters_and_merges(settings):
    small = settings.model_copy(update={"llm_chunk_chars": 10})
    llm = FakeLLM(routes={"PASS_A": PASS_A_JSON})
    out = extract_chapter(
        _runner(small, llm), settings=small, book_id="b1",
        chapter_index=1, title="第一章", content="第一句。第二句。第三句。",
    )
    assert len(llm.calls) == 2   # 10 字上限下"第一句。第二句。"合成一块，"第三句。"单独一块
    assert len(out.characters) == 6  # 两个分块各 3 个角色，去重留给聚合阶段


def test_merge_pass_a_concatenates_chunks():
    a = PassAOutput.model_validate({"characters": [{"name": "A"}], "relationships": []})
    b = PassAOutput.model_validate({"characters": [{"name": "B"}], "relationships": []})
    merged = merge_pass_a([a, b])
    assert [c.name for c in merged.characters] == ["A", "B"]
