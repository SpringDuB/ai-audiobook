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


PASS_B_SYSTEM = """你是中文小说的场景分析师。你只输出 JSON 对象，不输出解释、不输出 Markdown 代码块。
你要把一个章节切分成若干"场景"：地点/时间/出场人物或情绪基调发生明显变化的地方就是边界。
你只能使用给定的角色名单，不要创造新角色名。"""

PASS_B_FORMAT = """输出格式（严格遵守，不要增删字段）：
{"scenes":[{"index":1,"title":"简短场景名","summary":"一句话摘要","participants":["角色名"],
"tone":"喜悦|愤怒|悲伤|恐惧|厌恶|忧郁|惊讶|平静","tone_intensity":0.5,
"starts_with":"该场景第一句的前20字","ends_with":"该场景最后一句的前20字"}]}"""

PASS_B_RULES = """要求：
1. 场景数量控制在 1–8 个，宁可少切也不要碎切；
2. starts_with / ends_with 必须逐字抄写给定句子（只抄前 20 字以内即可），不要改写、不要加省略号；
3. participants 只能从"已知角色名单"里选，必须包含实际在场人物；
4. tone 是该场景的整体基调，tone_intensity 取 0–1。"""


def pass_b_user(chapter_index: int, title: str, character_names: list[str], numbered: str) -> str:
    return (
        "【PASS_B】\n"
        f"章节序号：{chapter_index}\n"
        f"章节标题：{title}\n"
        f"已知角色名单：{'、'.join(character_names)}\n\n"
        f"{PASS_B_RULES}\n\n"
        f"{PASS_B_FORMAT}\n\n"
        f"句子列表：\n{numbered}"
    )
