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
   - 【职责】语速、音量、气息、停顿、情绪全部写在这里：这个角色的基础音色
     （性别、年龄、嗓音粗细）由后面的选角环节另外写，你不要在 role 里加音色说明；
   - 【不要】写音色本身（年龄、嗓音粗细这类属于角色，不属于这一句），
     也不要复述台词内容、不要写"他在生气"这种第三人称分析，直接写怎么演；
   - 旁白也要写，例如"平稳叙述，语速中等"、"压低声音，语速稍慢，带点悬念"；
   - 台词很平、没有线索时写"平静地陈述，语速中等"这种短句就行，不必凑满字数。"""

EXTRACT_FORMAT = """输出格式（严格的 JSON 数组，不要包对象、不要增删字段）：
[{"text":"原文句子","role":"角色名或旁白","voice":"这一句的表演描述"}]
（仅当接口强制要求顶层必须是 JSON 对象时，才把数组放进 lines 字段：{"lines": [ ... ]}。
不要回显 response_format、不要加 type 之类的额外字段。）"""


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


# ---------------------------------------------------------------- 选角表 + 音色描述（VoiceDesign）
# 角色不再从音色库里挑：先出一张"选角表"（每个角色的音色原型，互相拉开距离），
# 再让大模型按原型给每个角色写"基础音色描述"（这个声音是什么样），
# 逐句合成时再和该句的"表演描述"（这一句怎么说）拼起来喂 Qwen3-TTS VoiceDesign。

VOICE_DESIGN_SYSTEM = """你是中文有声书的选角导演兼音色设计师。你只输出严格 JSON 对象，
不输出解释、不输出 Markdown 代码块。"""

VOICE_DESIGN_RULES = """根据角色的称呼、身份线索和台词样本，写两样东西：description 和 sample。

1. description：这个角色的**基础音色描述**，40–120 字，一段话写完。
   只写"这个声音天生是什么样"，按下面的顺序覆盖：
   - 性别与年龄感（例如"二十出头的年轻男性"）。性别或年龄从台词里推不出来时
     不要硬编，改成中性写法（例如"中性偏低的青年声线"）；
   - 音区：低音 / 中音 / 高音（必写）；
   - 音色质地与共鸣：清亮、沙哑、厚实、单薄、颗粒感、鼻音、磁性……（必写）；
   - 咬字与口音：吐字清楚、咬字偏松、尾音上挑、带点京腔……（有线索才写）。
   可以附一句"性格底色"，但只能用听得出音色的词（沉稳、清冷、柔和、锐利、松弛），
   并且它只是默认底色，不是每一句的语气。

   【不要写】语速、音量、气息、情绪、语气 —— 那些是"这一句怎么说"，逐句分析
   另外负责。写进来会和逐句指令打架（描述写"语速偏慢"、某一句又要求"语速偏快"）。

   【禁止】
   - 写任何真实演员、配音员、作品或公众人物的名字，也不要写"像某某的声音"；
   - 写设备和音质词（录音棚、HiFi、无损、降噪）；
   - 写互相矛盾的标签（沙哑＋清亮、低沉＋童声）；
   - 把台词原句抄进 description；
   - 出现"请你…""要…"这类指令句，description 必须是描述性的。

2. sample：一句用来试音的台词，15–40 字：
   - 优先从该角色的台词里原样截一句（长台词可以截连续的一段，但不要改字）；
   - 【重要】挑"平静说话"的那一句：不要挑喊叫、哭喊、嘶吼、极端情绪的句子，
     试听时听到的应该是这个角色的日常声音；
   - 台词都太短或没有台词时，自己写一句符合这个角色口吻的口语；
   - 不要引号、不要"某某说："前缀、不要括号里的动作提示。"""

NARRATOR_RULES = """注意：这个角色是**旁白/说书人**（负责整本书的叙述，不是剧中人物）。
- description 写成适合通读全书的讲述声：性别与年龄感、音区、嗓音质地、讲述感，
  例如"三十多岁的男性，中低音区，嗓音厚实，吐字清楚，有讲述感"；
- 不要写语速与情绪（逐句分析负责），也不要把旁白写成某个剧中人物的口吻；
- 避免"播音腔、朗诵腔、译制片腔"这类端着的形容，除非台词样本本身就是那个风格；
- sample 从旁白句里挑一句**陈述性**叙述（15–40 字）：别挑拟声、感叹、
  战斗或惊悚段落里的高张力句子，也别挑对白。"""

VOICE_DESIGN_FORMAT = """输出格式：{"description":"音色描述","sample":"试音台词"}"""

REFINE_RULES = """这一版是**微调**，不是重做：上一版描述用户已经在用了（角色音频都是按它生成的），
请至少保持 80% 不变 —— 音区、音色质地、性别与年龄感原则上不许动。只修这几类问题：
1. 缺必写维度（音区、质地）就补上；
2. 写了语速、音量、气息、情绪就删掉（改由逐句描述负责）；
3. 互相矛盾、没有台词依据或违反上面禁令的内容，修掉。
上一版没有明显问题时基本原样输出，只润色措辞。"""


# ---------------------------------------------------------------- 选角表（全局协调）
# 每个角色单独设计时彼此看不见，同类角色（多个青年男性/年轻女性）容易写出几乎
# 一样的描述，听众分不清谁在说话。这里先花一次调用给所有角色定"音色原型"，
# 强制两两之间在音区/年龄/质地/咬字上拉开距离，再让每个角色按原型细化。

CAST_SHEET_SYSTEM = """你是中文有声书的选角导演。你只输出严格 JSON 对象，不输出解释、不输出 Markdown 代码块。
你的任务：给这本书的每个角色定一个音色原型，保证听众能靠声音分清谁是谁。"""

CAST_SHEET_RULES = """给每个角色写一个**音色原型**（一句话，10–30 字）：音区 + 音色质地 + 年龄感。
硬性要求：
1. 任意两个角色之间，在「音区（低/中/高）」「年龄段」「音色质地」「咬字特点」
   四项里至少有 2 项不同 —— 尤其是性别年龄相近的角色，必须用音区（低 vs 高）
   和质地（沙哑 vs 清亮）拉开，不要都写"年轻女性的清亮嗓音"这种；
2. 旁白是讲述声，和所有剧中角色区分开（通常中低音区、吐字清楚）；
3. 只写音色（音区、质地、年龄感、咬字），不要写情绪、语速、台词内容；
4. 角色名必须与清单完全一致：不漏、不多、不改字；
5. 清单里标了"已定音色"的角色原样尊重它；另外给出的【已占用音色】是别的角色
   已经定下的原型（不在这一批里），也要绕着它们选，别撞车。"""

CAST_SHEET_FORMAT = """输出格式：{"characters":[{"name":"角色名","archetype":"音色原型"}]}"""


def cast_sheet_user(rows: list[dict], avoid: list[dict] | None = None) -> str:
    lines = []
    for row in rows:
        note = str(row.get("note") or "").strip()
        tail = f"　{note}" if note else ""
        lines.append(f"{row.get('name')}（{row.get('lines', 0)} 句）{tail}")
    parts = ["【CAST_SHEET】", CAST_SHEET_RULES, CAST_SHEET_FORMAT, "【角色清单】\n" + "\n".join(lines)]
    if avoid:
        occupied = "\n".join(f"- {item['name']}：{item['archetype']}" for item in avoid if item.get("archetype"))
        if occupied:
            parts.append("【已占用音色（不在这一批里，别撞）】\n" + occupied)
    return "\n\n".join(parts)


def _avoid_section(avoid: list[dict] | None, name: str = "") -> str:
    """已占用音色：给单个角色的描述生成用（出场少、不参与选角表的角色别撞主角群）。"""
    rows = [item for item in (avoid or []) if item.get("archetype") and item.get("name") != name]
    if not rows:
        return ""
    body = "\n".join(f"- {item['name']}：{item['archetype']}" for item in rows)
    return f"【已占用音色（别和这些角色撞）】\n{body}"


DIRECTIVE_RULES = """【用户要求（硬性）】用户点名要这个声音满足下面的要求，必须落进 description：
{directive}
- 即使和你从台词里推断出来的不一致，也以用户要求为准；
- 要求里如果提到语速、语气这类"这一句怎么说"的东西（例如"说话慢一点""凶一点"），
  把它翻译成音色层面的写法（"咬字舒缓、字间留白"、"音色冷硬、共鸣靠前"），
  不要直接写语速和情绪；
- 格式与上面的禁令照旧：不写真人、不写设备音质词、不抄台词、不写矛盾和指令句。"""


def voice_design_user(
    name: str,
    aliases: list[str] | None,
    samples: list[str],
    lines: int = 0,
    *,
    is_narrator: bool = False,
    clues: list[str] | None = None,
    archetype: str = "",
    previous: str = "",
    directive: str = "",
    avoid: list[dict] | None = None,
) -> str:
    quoted = " | ".join(samples) if samples else "（这个角色没有单独的台词，主要出现在叙述里）"
    alias_text = "、".join(aliases or []) or "（无）"
    parts = ["【VOICE_DESIGN】", VOICE_DESIGN_RULES]
    if is_narrator:
        parts.append(NARRATOR_RULES)
    parts.append(VOICE_DESIGN_FORMAT)
    if archetype:
        parts.append("【选角表】这个角色的音色原型（必须遵守，别和其他角色撞车）：" + archetype)
    section = _avoid_section(avoid, name)
    if section:
        parts.append(section)
    if previous:
        parts.append(REFINE_RULES)
        parts.append("上一版描述：" + previous)
    if directive:
        parts.append(DIRECTIVE_RULES.format(directive=directive))
    parts.append(f"角色：{name}\n别名：{alias_text}\n台词数：{lines}\n台词样本：{quoted}")
    if clues:
        parts.append("【旁白里对这个角色的描写（推断身份用，不是台词）】\n" + "\n".join(clues))
    return "\n\n".join(parts)
