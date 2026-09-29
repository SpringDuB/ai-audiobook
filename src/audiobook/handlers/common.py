"""handler 之间共用的小工具（放在这里避免 handler 之间循环 import）。"""


def require_llm(ctx):
    if ctx.llm is None:
        raise RuntimeError("worker 未配置 LLM：请设置 AB_LLM_BASE_URL 后重启 worker")
    return ctx.llm
