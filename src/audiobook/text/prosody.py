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

# 情绪对语速的影响：低落/忧郁要慢，激动/紧张要快（乘在 delivery 之上，整体限幅 ±15%）
RATE_BY_EMOTION = {
    "喜悦": 1.04,
    "愤怒": 1.06,
    "悲伤": 0.93,
    "恐惧": 1.05,
    "厌恶": 0.98,
    "忧郁": 0.92,
    "惊讶": 1.06,
    "平静": 1.0,
}

TRAILING_MARKS = "”\"’』」）)】]"


def derive_pause_ms(text: str, intensity: float | None = None) -> int:
    """句子之间的停顿（毫秒）：按收尾标点定基准，情绪越强留得越久一点。"""
    # 中文对白常以引号收尾（“你为什么要杀我？”），判定标点前先剥掉这些收尾符号
    stripped = text.rstrip().rstrip(TRAILING_MARKS).rstrip()
    base = 200
    for token, ms in PUNCT_PAUSE:
        if stripped.endswith(token):
            base = ms
            break
    if intensity is not None and intensity >= 0.8:
        base += 150
    return base


def derive_rate(delivery: str = "normal", emotion: dict | None = None) -> float:
    """语速：delivery 打底，情绪再微调；>1 偏快（IndexTTS 侧换算成 duration_factor 的倒数）。"""
    rate = RATE_BY_DELIVERY.get((delivery or "normal").lower(), 1.0)
    if isinstance(emotion, dict):
        rate *= RATE_BY_EMOTION.get(emotion.get("dominant"), 1.0)
    return round(max(0.85, min(1.15, rate)), 3)
