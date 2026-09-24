PUNCT_PAUSE = [
    ("……", 800),
    ("——", 400),
    ("！", 350),
    ("？", 350),
    ("!", 350),
    ("?", 350),
    ("。", 300),
    (".", 300),
    ("；", 200),
    (";", 200),
    ("，", 120),
    (",", 120),
]

RATE_BY_DELIVERY = {"shout": 1.05, "whisper": 0.92, "sneer": 0.97, "normal": 1.0}

TRAILING_MARKS = "”\"’』」）)】]"


def derive_pause_ms(text: str, scene_switch: bool = False, intensity: float | None = None) -> int:
    # 中文对白常以引号收尾（“你为什么要杀我？”），判定标点前先剥掉这些收尾符号
    stripped = text.rstrip().rstrip(TRAILING_MARKS).rstrip()
    base = 200
    for token, ms in PUNCT_PAUSE:
        if stripped.endswith(token):
            base = ms
            break
    if scene_switch:
        base += 500
    if intensity is not None and intensity >= 0.8:
        base += 150
    return base


def derive_rate(delivery: str = "normal", intensity: float | None = None) -> float:
    return RATE_BY_DELIVERY.get((delivery or "normal").lower(), 1.0)
