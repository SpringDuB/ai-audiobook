PASS_A_SYSTEM = """你是中文小说的角色分析师。你只输出 JSON 对象，不输出解释、不输出 Markdown 代码块。
你的任务是：从给定章节中找出所有具体角色，抽取他们的别名、性别、年龄段、性格底色与说话习惯，
并给出本章中能确定的有向人物关系。找不到的信息一律用默认值，不要编造。"""

PASS_A_FORMAT = """输出格式（严格遵守，不要增删字段）：
{"characters":[{"name":"","aliases":[],"gender":"男|女|中性|未知",
"age_group":"儿童|少年|青年|中年|老年|未知","personality":["最多4个形容词"],
"speaking_style":"","base_emotion":"喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静","base_intensity":0.4}],
"relationships":[{"from":"","to":"","closeness":0.5,"hierarchy":0.5,"hostility":0.0,"intimacy":0.0,"note":""}]}"""

PASS_A_RULES = """要求：
1. 只输出有台词或明确推动情节的**具体人物**；"黑衣人""神秘人""那个男人"这类泛称不要输出；
2. 旁白必须输出，name 固定为"旁白"，gender/age_group 用"未知"；
3. 同一人物的不同称呼（如"秦少""老秦"）写进 aliases，不要重复建角色；
4. relationships 的 from/to 用 name 字段里的名字；四个维度取值 0–1，hostility 表示敌意、intimacy 表示亲密；
5. 只依据本章原文，不引入其他章节的推断。"""


def pass_a_user(chapter_index: int, title: str, text: str) -> str:
    return (
        "【PASS_A】\n"
        f"章节序号：{chapter_index}\n"
        f"章节标题：{title}\n\n"
        f"{PASS_A_RULES}\n\n"
        f"{PASS_A_FORMAT}\n\n"
        f"正文：\n{text}"
    )


PASS_C_SYSTEM = """你是中文有声书的配音导演。你只输出 JSON 对象，不输出解释、不输出 Markdown 代码块。
你的唯一目标：让每一句都带上"该怎么演"的信息 —— 谁在说、对谁说、什么情绪、多大幅度、怎么说。
这段分析会直接驱动 TTS 合成，旁白太平、人物念稿都是失败。"""

PASS_C_FORMAT = """输出格式（严格遵守，不要增删字段，每条句子都要有一条记录）：
{"lines":[{"index":1,"speaker":"角色名或旁白","addressee":"角色名或null",
"emotion":"喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静",
"intensity":0.5,"secondary":"以上枚举之一或null","secondary_weight":0.0,
"delivery":"normal|shout|whisper|sneer","emotion_text":"这句该怎么演（≤25字）"}]}"""

PASS_C_RULES = """情绪与表演要求：
1. 每句都必须给 emotion，不许全部写"平静"：旁白也有语气（紧张、沉重、轻快、冷峻、温柔……映射到最接近的枚举），
   旁白强度一般在 0.2–0.5；对白按人物此刻真实的心情给，冲突、爆发、生死关头可以给到 0.85–1.0。
2. 判断对白情绪时，必须同时看三样东西：说话人的性格底色、他对**听话人**的态度（关系里的亲疏/尊卑/敌意/亲密）、
   以及这句话当下的处境。同一句话对爱人、对仇人、对上司要给出不一样的情绪和幅度。
3. intensity 是表演幅度，不是情绪好坏：日常寒暄 0.3–0.5；明显情绪外露 0.6–0.8；失控/嘶吼/崩溃 0.85–1.0。
   同一章里不要所有句子都填同一个数值。
4. 一句话里有两层情绪时用 secondary：写"藏在表面底下的那一层"，secondary_weight 取 0.1–0.5。
   例：笑着威胁 → emotion 喜悦 intensity 0.6、secondary 愤怒 secondary_weight 0.35；
   强撑镇定 → emotion 平静 intensity 0.5、secondary 恐惧 secondary_weight 0.4。没有第二层就写 null 和 0。
5. delivery：喊叫/嘶吼/怒吼 shout；耳语/压低声音/虚弱 whisper；冷笑/讥讽/阴阳怪气 sneer；其余 normal。
6. 心理活动、回忆、内心独白算"旁白"，但要按内容给情绪，不要一律平静。
7. emotion_text 是给配音演员的一句话指令（≤25 字），写"怎么演"而不是重复情绪词，不要留空。
   例：「压着火气，语速比平时快」「疲惫但温柔，尾音发虚」「平铺直叙，像在回忆旧事」「故作轻松，其实心虚」。
   旁白同样要给：旁白决定整本书的听感，不能写成"平静地朗读"。

说话人判定：
8. speaker 只能从"本段角色"里选，或写"旁白"；不要发明新名字。引号内的直接引语必须有具体说话人；
   对话里的称呼（"老苏""王大人"）可以提示 speaker 和 addressee。
9. addressee 只能是本段在场角色；确实判断不出来写 null。
10. 只依据原文判断，不要编造情节；实在判断不了情绪时才写"继承"（表示沿用上一句）。"""


def pass_c_user(chapter_index: int, title: str, numbered: str, context_block: str) -> str:
    return (
        "【PASS_C】\n"
        f"章节序号：{chapter_index}\n"
        f"章节标题：{title}\n"
        "编号规则：下面每句前面的数字就是“本章第几句”，作答时必须使用同样的编号。\n\n"
        f"{context_block}\n\n"
        f"{PASS_C_RULES}\n\n"
        f"{PASS_C_FORMAT}\n\n"
        f"句子列表：\n{numbered}"
    )
