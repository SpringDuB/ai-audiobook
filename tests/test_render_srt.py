import pytest

from audiobook.render.srt import Cue, format_time, parse_srt, render_srt, shift_cues, write_srt


def test_format_time_matches_srt_convention():
    assert format_time(0) == "00:00:00,000"
    assert format_time(3661.5) == "01:01:01,500"
    assert format_time(-1) == "00:00:00,000"


def test_write_then_parse_round_trip(tmp_path):
    cues = [Cue(0.0, 1.25, "第一句"), Cue(1.3, 2.0, "第二句\n续行")]
    path = tmp_path / "a.srt"
    write_srt(cues, path)
    assert parse_srt(path) == cues


def test_parse_accepts_dot_decimal_and_missing_blank_line(tmp_path):
    path = tmp_path / "b.srt"
    path.write_text(
        "1\n00:00:01.500 --> 00:00:02.000\n甲\n\n2\n00:00:03,000 --> 00:00:04,000\n乙\n",
        encoding="utf-8",
    )
    cues = parse_srt(path)
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [(1.5, 2.0, "甲"), (3.0, 4.0, "乙")]


def test_parse_skips_header_junk(tmp_path):
    path = tmp_path / "c.srt"
    path.write_text("WEBVTT\n\n1\n00:00:01,000 --> 00:00:02,000\n甲\n", encoding="utf-8")
    assert [cue.text for cue in parse_srt(path)] == ["甲"]


def test_shift_cues_moves_and_clamps_to_chapter_end():
    cues = [Cue(0.0, 1.0, "甲"), Cue(0.9, 1.4, "乙"), Cue(1.5, 2.0, "丙")]
    shifted = shift_cues(cues, offset=10.0, limit=11.0)
    assert [(round(c.start, 3), round(c.end, 3)) for c in shifted] == [(10.0, 11.0), (10.9, 11.0)]
    assert [cue.text for cue in shifted] == ["甲", "乙"]


def test_render_srt_renumbers_from_one():
    text = render_srt([Cue(0.0, 1.0, "甲"), Cue(1.0, 2.0, "乙")])
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,000\n甲")
    assert "\n2\n00:00:01,000 --> 00:00:02,000\n乙\n" in text


def test_audio_write_srt_still_accepts_legacy_tuples(tmp_path):
    from audiobook import audio

    path = tmp_path / "legacy.srt"
    audio.write_srt([(0.0, 1.0, "甲")], path)
    assert "1\n00:00:00,000 --> 00:00:01,000\n甲" in path.read_text(encoding="utf-8")
    assert audio.format_srt_time(1.0) == "00:00:01,000"
    with pytest.raises(OSError):
        parse_srt(tmp_path / "missing.srt")
