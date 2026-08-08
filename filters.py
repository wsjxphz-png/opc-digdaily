"""
确定性内容过滤器（不依赖 LLM 判读）

用户明确偏好：完全不懂代码、没有技术背景，因此**凡是偏技术的一人公司
（写代码 / 编程 / 独立开发者 / SaaS 开发 / 技术大牛 / 技术博主 / 程序员 …）
都不要进入「操盘手案例」和「赚钱机会文章」**。

LLM 偶尔会把技术大牛误判成「无技术门槛」而放行，所以这里再加一道
与 AI 无关的、基于关键词的硬过滤：只要候选的名字 / 简介 / 理由里出现
强技术信号，直接判死，AI 再「自觉」也绕不过去。

注意：关键词刻意只收「强技术信号」（如「写代码」「程序员」「独立开发者」
「SaaS 开发」），避免使用「开发」「技术」这种在商业语境里也常见、会误伤
合法非技术 OPC 的泛词。
"""

# 强技术信号关键词（大小写不敏感，子串匹配）
TECH_KEYWORDS: list[str] = [
    # 中文 - 编程 / 程序员身份
    "写代码", "编程", "程序员", "码农", "程序媛",
    "软件工程师", "软件开发", "全栈工程师", "全栈开发",
    "后端工程师", "后端开发", "前端工程师", "前端开发",
    "算法工程师", "技术总监",
    "技术大牛", "技术大佬", "技术博主", "技术宅", "技术大v", "技术出身",
    "程序员出身", "搞技术", "敲代码", "写程序",
    # 中文 - 独立开发 / 开源 / AI 工程
    "独立开发者", "开源作者", "开源项目",
    "技术架构师", "软件架构师",
    "ai 工程师", "机器学习", "深度学习",
    "大模型工程师", "大模型训练",
    "saas 开发", "开发 saas", "卖代码", "代码模板",
    "编程教学", "技术教程",
    # 英文
    "software engineer", "full-stack", "full stack",
    "backend engineer", "frontend engineer", "back-end", "front-end",
    "open source", "open-source", "indie hacker", "tech blogger",
    "ai engineer", "machine learning", "deep learning",
    "writes code", "codes in", "github", "stack overflow",
]


def is_technical(text: str) -> bool:
    """判断一段文本是否包含「强技术信号」。

    用于把技术向的一人公司 / 文章确定性地挡在推送之外，
    与 LLM 给出的 tech_barrier 判读形成双保险。
    """
    if not text:
        return False
    t = text.lower()
    for kw in TECH_KEYWORDS:
        if kw.lower() in t:
            return True
    return False
