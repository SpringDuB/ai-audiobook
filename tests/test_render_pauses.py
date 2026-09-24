from audiobook.config import get_settings
from audiobook.render.pauses import build_pause_plan, effective_pause_ms


def _row(text, scene="c0001-s01", intensity=0.3, **extra):
    return {"text": text, "scene": scene, "emotion": {"intensity": intensity}, **extra}


def test_scene_boundary_gets_extra_pause_on_previous_line(settings):
    rows = [_row("第一句。"), _row("第二句。", scene="c0001-s02")]
    assert effective_pause_ms(rows[0], rows[1], settings) == 800   # 300 + 500
    assert effective_pause_ms(rows[1], None, settings) == 300      # 章节最后一句不加切换停顿


def test_pause_scale_and_clamp_are_applied(settings):
    loud = get_settings(data_dir=settings.data_dir, pause_scale=2.0, pause_max_ms=900)
    assert effective_pause_ms(_row("省略号……"), None, loud) == 900          # 800*2 被上限压到 900
    quiet = get_settings(data_dir=settings.data_dir, pause_scale=0.1, pause_min_ms=80)
    assert effective_pause_ms(_row("逗号，"), None, quiet) == 80              # 120*0.1 被下限抬到 80


def test_override_wins_over_derivation(settings):
    assert effective_pause_ms(_row("第一句。", pause_override_ms=1500), None, settings) == 1200
    assert effective_pause_ms(_row("第一句。", pause_override_ms=40), None, settings) == 80


def test_high_intensity_adds_pause(settings):
    assert effective_pause_ms(_row("滚！", intensity=0.9), None, settings) == 500   # 350 + 150


def test_build_pause_plan_matches_per_line_results(settings):
    rows = [_row("一。"), _row("二！"), _row("三，", scene="c0001-s02")]
    assert build_pause_plan(rows, settings) == [
        effective_pause_ms(rows[0], rows[1], settings),
        effective_pause_ms(rows[1], rows[2], settings),
        effective_pause_ms(rows[2], None, settings),
    ]
