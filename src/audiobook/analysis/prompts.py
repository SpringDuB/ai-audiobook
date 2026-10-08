"""分析链的提示词：提取 → 角色整合 → 音色描述（VoiceDesign）/ 库存音色推荐。

分句、判断说话人、合并同人异名、写音色描述全部交给大模型；
代码只在提示词里写清楚输出契约，并对返回值做校验。

情绪不再进模型输出：Qwen3-TTS 不走向量通道。语气拆成两层 ——
「这一句怎么说」由提取阶段逐句直出（voice 字段），「这个角色是什么声音」
由音色描述阶段每个角色写一次（description 字段），合成时拼在一起。
"""

# ---------------------------------------------------------------- 提取

EXTRACT_SYSTEM = """你是中文小说的配音改编助手。你只输出严格 JSON 数组，不输出解释、不输出 Markdown 代码块。
你的任务：把给定的小说片段逐句拆开，判断每一句话是谁说的，并写出这句话该怎么说。"""

EXTRACT_RULES = """规则：
1. 叙述性文字标记为"旁白"；
2. 对话内容根据上下文推断说话角色；
3. 如果无法确定角色，标记为"未知"；
4. 保持原文完整性，不要省略或改写文本内容；
5. 【强制】若原文以"X："或"X:"开头（X 为角色名），则该句 role 必须是 X，
   且 text 字段绝对不要包含"X："前缀，只保留纯台词内容。
   示例：原文"小鹿：多人？还能自己生成？"必须输出 {"text":"多人？还能自己生成？","role":"小鹿"}，
   禁止输出 {"text":"小鹿：多人？还能自己生成？","role":"旁白"}；
6. 逐句输出，不要合并或拆分原文的句子；除规则 5 允许去掉的"X："前缀外，text 必须与原文逐字一致；
7. 同一个人在全章保持同一个称呼；拿不准就写"未知"，不要编造角色名；
8. voice 是"这一句该怎么说"的表演描述，12–30 字，一句话写完：
   - 写语气、情绪、语速、音量、气息、停顿，例如
     "压低声音，语速放慢，尾音有点发抖"、"不耐烦地提高音量，语速偏快"；
   - 【不要】写音色本身（年龄、嗓音粗细这类属于角色，不属于这一句），
     也不要复述台词内容、不要写"他在生气"这种第三人称分析，直接写怎么演；
   - 旁白也要写，例如"平稳叙述，语速中等"、"压低声音，语速稍慢，带点悬念"；
   - 台词很平、没有线索时写"平静地陈述，语速中等"。"""

EXTRACT_FORMAT = """输出格式（严格的 JSON 数组，不要包对象、不要增删字段）：
[{"text":"原文句子","role":"角色名或旁白","voice":"这一句的表演描述"}]"""


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


# ---------------------------------------------------------------- 音色描述（VoiceDesign）
# 角色不再从音色库里挑：大模型给每个角色写一段"基础音色描述"（声音是什么样），
# 逐句合成时再和该句的"表演描述"（这一句怎么说）拼起来喂 Qwen3-TTS VoiceDesign。

VOICE_DESIGN_SYSTEM = """你是中文有声书的选角导演兼音色设计师。你只输出严格 JSON 对象，
不输出解释、不输出 Markdown 代码块。"""

VOICE_DESIGN_RULES = """根据角色的称呼、身份线索和台词样本，写两样东西：

1. description：这个角色的**基础音色描述**，40–120 字，要能直接喂给语音设计模型。写清：
   - 性别与年龄感（例如"二十出头的年轻男性"）；
   - 音色质地（例如"嗓音偏低、略带沙哑"）；
   - 说话习惯（语速快慢、咬字松紧、气息强弱）；
   - 性格气质（例如"慵懒随性、带点痞气"）。
   括号里只是示例，要结合角色本身写，不要照抄。
   【注意】这里只写"这个声音本身是什么样"，不要写某一句的情绪 ——
   每句话的语气由逐句分析另外负责。

2. sample：一句用来试音的台词，15–40 字：
   - 优先从该角色的台词里原样挑一句；
   - 台词都太短或没有台词时，自己写一句符合这个角色口吻的口语；
   - 【重要】挑"平静说话"的那一句：不要挑喊叫、哭喊、嘶吼、极端情绪的句子，
     试听时听到的应该是这个角色的日常声音。

规则：
1. 不要参照任何具体演员、配音员、作品或真实人物的名字；
2. 不要写"像某某的声音"这类描述；
3. 不要把没有依据的设定编进 description（台词里看不出的身份别硬加）；
4. 输出只有 description 与 sample 两个字段，都是字符串。"""

NARRATOR_RULES = """注意：这个角色是**旁白/说书人**（负责整本书的叙述，不是剧中人物）。
- description 要写成适合通读全书的讲述声：性别与年龄感、嗓音质地、语速与讲述感，
  例如"三十多岁的男性，嗓音低沉厚实，语速中偏慢，吐字清楚，讲述感强"；
- sample 从旁白句里挑一句平缓叙述的（15–40 字），别挑对白；
- 不要把旁白写成某个剧中人物的口吻。"""

VOICE_DESIGN_FORMAT = """输出格式：{"description":"音色描述","sample":"试音台词"}"""


def voice_design_user(
    name: str,
    aliases: list[str] | None,
    samples: list[str],
    lines: int = 0,
    *,
    is_narrator: bool = False,
) -> str:
    quoted = " | ".join(samples) if samples else "（这个角色没有单独的台词，主要出现在叙述里）"
    alias_text = "、".join(aliases or []) or "（无）"
    parts = ["【VOICE_DESIGN】", VOICE_DESIGN_RULES]
    if is_narrator:
        parts.append(NARRATOR_RULES)
    parts.append(VOICE_DESIGN_FORMAT)
    parts.append(f"角色：{name}\n别名：{alias_text}\n台词数：{lines}\n台词样本：{quoted}")
    return "\n\n".join(parts)
