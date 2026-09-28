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


CHAPTER_ANALYSIS_SYSTEM = """你是中文有声书的改编导演。你只输出 JSON 对象，不输出解释、不输出 Markdown 代码块。
读一遍这一章，同时产出两样东西：
1) 本章出现的具体角色（含别名、性别、年龄段、性格底色、说话习惯）与他们之间的关系；
2) 本章每一句话的配音标注：谁在说、什么情绪、多大幅度、怎么说。

输入已经把每句标成 [旁白] 或 [对白]：引号里的直接引语是 [对白]，其余是 [旁白]。
这两件事必须一起做：角色表是你判断"谁在说话"的依据，全章上下文是你判断"这句话什么情绪"的依据。
情绪要贴着剧情走——把对白标成旁白、或让所有句子都"平静"，都算失败。"""

CHAPTER_ANALYSIS_FORMAT = """输出格式（严格遵守，不要增删字段）：
{"characters":[{"name":"","aliases":[],"gender":"男|女|中性|未知",
"age_group":"儿童|少年|青年|中年|老年|未知","personality":["最多4个形容词"],
"speaking_style":"","base_emotion":"喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静","base_intensity":0.4}],
"relationships":[{"from":"","to":"","closeness":0.5,"hierarchy":0.5,"hostility":0.0,"intimacy":0.0,"note":""}],
"lines":[{"index":1,"speaker":"角色名或旁白",
"emotion":"喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静",
"intensity":0.5,"secondary":"以上枚举之一或null","secondary_weight":0.0,
"delivery":"normal|shout|whisper|sneer","emotion_text":"这句该怎么演（≤25字）"}]}"""

CHAPTER_ANALYSIS_RULES = """角色部分：
1. 只输出有台词或明确推动情节的**具体人物**；"黑衣人""神秘人""那个男人"这类泛称不要输出；
2. 旁白必须输出，name 固定为"旁白"，gender/age_group 用"未知"；
3. 同一人物的不同称呼（"秦少""老秦"）写进 aliases，不要重复建角色；lines 里的 speaker 用 name 字段那个名字；
4. relationships 的 from/to 用 name 字段里的名字；四个维度取值 0–1，hostility 表示敌意、intimacy 表示亲密；

说话人判定（最重要，先做完这一步再判情绪）：
5. [对白] 行必须给具体角色名，不许写"旁白"——引语一定是某个人物说出来的。判断顺序：
   a. 行首的说话人提示（如 [对白｜说话人提示：王胖子]）来自原文归属句，直接采用；
   b. 没有提示时看上下文：前一句的"X 说道/问道"、后一句的"X 一听，答道"、对话里对彼此的称呼；
   c. 整段实在没有线索时才写"旁白"——这是兜底，正常一章里不该超过几条。
6. [旁白] 行是叙述文字，speaker 一律写"旁白"，不要写成角色；心理活动、回忆、内心独白也是旁白，不要改成角色；
7. 相邻的 [对白] 行通常是一来一回的两方，但不要机械交替：以归属句和称呼为准；
8. speaker 只能写 characters 里出现过的 name，或写"旁白"；不要发明新名字，也不要写"未知""某人"。

情绪与表演要求：
9. 每句都必须给 emotion，不许全部写"平静"：旁白也有语气（紧张、沉重、轻快、冷峻、温柔……映射到最接近的枚举），
   旁白强度一般在 0.2–0.5；对白按人物此刻真实的心情给，冲突、爆发、生死关头可以给到 0.85–1.0；
10. 判情绪时同时看三样：说话人的性格底色、本段人物关系（亲疏/尊卑/敌意/亲密）、以及这句话当下的处境；
11. intensity 是表演幅度，不是情绪好坏：日常寒暄 0.3–0.5；明显情绪外露 0.6–0.8；失控/嘶吼/崩溃 0.85–1.0。
    同一章里不要所有句子都填同一个数值；
12. 一句话里有两层情绪时用 secondary：写"藏在表面底下的那一层"，secondary_weight 取 0.1–0.5。
    例：笑着威胁 → emotion 喜悦 intensity 0.6、secondary 愤怒 secondary_weight 0.35；
    强撑镇定 → emotion 平静 intensity 0.5、secondary 恐惧 secondary_weight 0.4。没有第二层就写 null 和 0；
13. delivery：喊叫/嘶吼/怒吼 shout；耳语/压低声音/虚弱 whisper；冷笑/讥讽/阴阳怪气 sneer；其余 normal；
14. emotion_text 是给配音演员的一句话指令（≤25 字），写"怎么演"而不是重复情绪词，不要留空。
    例：「压着火气，语速比平时快」「疲惫但温柔，尾音发虚」「平铺直叙，像在回忆旧事」「故作轻松，其实心虚」。
    旁白同样要给：旁白决定整本书的听感，不能写成"平静地朗读"；
15. 只依据原文判断，不要编造情节；实在判断不了情绪时才写"继承"（表示沿用上一句）。"""


def chapter_analysis_user(
    chapter_index: int,
    title: str,
    numbered: str,
    *,
    chapter_text: str = "",
    known_characters: list[str] | None = None,
) -> str:
    parts = [
        "【CHAPTER_ANALYSIS】",
        f"章节序号：{chapter_index}",
        f"章节标题：{title}",
        "编号规则：下面每句前面的数字就是“本章第几句”，作答时必须使用同样的编号，一行都不能漏。",
        "每行格式：编号. [旁白] 或 [对白｜说话人提示：角色名] + 句子内容；说话人提示来自原文归属句，可以直接采用。",
    ]
    if known_characters:
        parts.append("本章前文已经出场的角色（名字保持一致，别换叫法）：" + "、".join(known_characters))
    if chapter_text:
        parts.append("以下是本章全文（只作上下文，不要标注这里的内容）：\n" + chapter_text)
    parts.append(CHAPTER_ANALYSIS_RULES)
    parts.append(CHAPTER_ANALYSIS_FORMAT)
    parts.append(f"需要标注的句子：\n{numbered}")
    return "\n\n".join(parts)


