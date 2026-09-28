"""分析链的三段提示词：提取 → 角色整合 → 音色推荐。

分句、判断说话人、合并同人异名、挑音色全部交给大模型；
代码只在提示词里写清楚输出契约，并对返回值做校验。
"""

# ---------------------------------------------------------------- 提取

EXTRACT_SYSTEM = """你是中文小说的配音改编助手。你只输出严格 JSON 数组，不输出解释、不输出 Markdown 代码块。
你的任务：把给定的小说片段逐句拆开，判断每一句话是谁说的，并给人物的话术配上情绪。"""

EXTRACT_RULES = """规则：
1. 叙述性文字标记为"旁白"；
2. 对话内容根据上下文推断说话角色；
3. 如果无法确定角色，标记为"未知"；
4. 保持原文完整性，不要省略或改写文本内容；
5. 【强制】若原文以"X："或"X:"开头（X 为角色名），则该句 role 必须是 X，
   且 text 字段绝对不要包含"X："前缀，只保留纯台词内容。
   示例：原文"小鹿：多人？还能自己生成？"必须输出 {"text":"多人？还能自己生成？","role":"小鹿"}，
   禁止输出 {"text":"小鹿：多人？还能自己生成？","role":"旁白"}；
6. 情绪只给人物的话术：emotion 从 喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静 里选一个；
   旁白不需要情绪，emotion 一律填 null；只有人物话术必须有情绪；
7. intensity 是表演幅度（0–1）：日常 0.3–0.5，情绪明显 0.6–0.8，失控/嘶吼 0.85–1.0；旁白填 null；
8. 一句话里有两层情绪时，secondary 写藏在表面底下的那一层，secondary_weight 取 0.1–0.5；没有就填 null；
9. 逐句输出，不要合并或拆分原文的句子；除规则 5 允许去掉的"X："前缀外，text 必须与原文逐字一致；
10. 同一个人在全章保持同一个称呼；拿不准就写"未知"，不要编造角色名。"""

EXTRACT_FORMAT = """输出格式（严格的 JSON 数组，不要包对象、不要增删字段）：
[{"text":"原文句子","role":"角色名或旁白","emotion":"喜悦","intensity":0.6,"secondary":null,"secondary_weight":null}]"""


def extract_user(
    chapter_index: int,
    title: str,
    text: str,
    *,
    known_roles: list[str] | None = None,
    window: int | None = None,
    windows: int | None = None,
) -> str:
    parts = ["【EXTRACT】", f"章节序号：{chapter_index}", f"章节标题：{title}"]
    if window and windows and windows > 1:
        parts.append(f"这是本章的第 {window}/{windows} 段，只标注这一段，前面/后面的内容由其它段负责。")
    if known_roles:
        parts.append("本书已经出现过的角色名（保持一致，别换叫法）：" + "、".join(known_roles))
    parts.append(EXTRACT_RULES)
    parts.append(EXTRACT_FORMAT)
    parts.append("【正文】\n" + text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 角色整合

MERGE_SYSTEM = """你是中文小说的角色统筹。你只输出严格 JSON 对象，不输出解释、不输出 Markdown 代码块。
给你一份"称呼 + 样本台词"清单，请把同一个人的不同称呼（本名/小名/绰号/尊称/简称）合并成同一个角色。"""

MERGE_RULES = """规则：
1. 只有确定是同一个人时才合并；拿不准就分开，宁可多一个角色，也不要错并；
2. name 取最正式、最常用的称呼，其余写进 aliases；
3. 每个称呼最多出现在一条记录里；name 与 aliases 都必须是清单里出现过的称呼；
4. "旁白"不是人物，不要写进结果；"未知"也不要并进任何角色；
5. 不要编造清单里没有的称呼。"""

MERGE_FORMAT = """输出格式：{"characters":[{"name":"主名","aliases":["其他称呼"]}]}"""


def merge_user(entries: list[dict], known: list[str] | None = None) -> str:
    rows = []
    for entry in entries:
        samples = " | ".join(entry.get("samples") or [])
        chapters = "、".join(str(index) for index in (entry.get("chapters") or []))
        suffix = f"，第 {chapters} 章" if chapters else ""
        rows.append(f"{entry['name']}（{entry.get('count', 0)} 句{suffix}）：{samples}")
    parts = ["【MERGE_ROLES】", MERGE_RULES, MERGE_FORMAT]
    if known:
        parts.append(
            "已经确认过的角色（下面的称呼如果其实是他们，请把 name 写成下面这个名字，"
            "把新称呼写进 aliases）：" + "、".join(known)
        )
    parts.append("【称呼与样本台词】\n" + "\n".join(rows))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 音色推荐

RECOMMEND_SYSTEM = """你是中文有声书的选角导演。你只输出严格 JSON 对象，不输出解释、不输出 Markdown 代码块。"""

RECOMMEND_RULES = """为下面这个角色推荐 1-3 个音色，只从【音色库】里选，按推荐程度从高到低排序。
规则：
1. voiceId 必须是【音色库】里出现过的 id，不要编造，也不要推荐库外的音色；
2. 依据角色的性别、年龄、身份、性格与台词语气，挑标签最贴合的音色；
3. confidence 取 0–1，表示你有多确定；
4. reason 写一句不超过 30 字的理由；
5. 拿不准就只推荐 1 个。"""

RECOMMEND_FORMAT = """输出格式：{"recommendations":[{"voiceId":"v001","confidence":0.95,"reason":"理由"}]}"""


def recommend_user(name: str, samples: list[str], catalog: str) -> str:
    lines = " | ".join(samples) if samples else "（这个角色没有单独的台词，主要是叙述）"
    return "\n\n".join(
        [
            "【VOICE_RECOMMEND】",
            RECOMMEND_RULES,
            RECOMMEND_FORMAT,
            f"角色：{name}\n台词：{lines}",
            "【音色库】\n" + catalog,
        ]
    )
