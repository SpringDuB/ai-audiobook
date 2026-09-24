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
