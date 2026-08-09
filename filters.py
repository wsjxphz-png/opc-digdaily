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


# ============================================================
# 语言陷阱 / 噱头检测（来源：dbs 商业本体论「第一层：语言陷阱检测」）
# ============================================================
#
# dbs 的诊断漏斗第一层就是查「模糊的、没有被定义的核心词」：
# 「适合」「值得」「应该」「好的」「高级」「有前景」「赛道」。
# 一段文字如果通篇都靠这类词撑着，说明作者自己也没想清楚在说什么，
# 这种内容对「照着抄」的读者是纯噪音。
#
# 关键设计：**按密度判定，不按单个命中判定。**
# 「风口」「机会」这些词在正常商业文章里也会出现一两次，单词命中会大量误伤。
# 只有当这类空词高频堆叠、而全文又拿不出具体数字时，才判为噱头。

# 没有定义的空词（dbs 语言陷阱词 + 中文自媒体常见的营销套话）
HYPE_WORDS: list[str] = [
    # dbs 原始陷阱词
    "赛道", "有前景", "很有前途", "值得做", "适合你", "适不适合",
    "高级感", "高大上",
    # 中文内容农场高频空词
    "风口", "红利期", "新蓝海", "蓝海市场", "下一个风口",
    "颠覆", "革命性", "划时代", "重新定义", "弯道超车",
    "财富自由", "躺赚", "睡后收入", "被动收入神话",
    "月入十万", "月入百万", "轻松月入", "月入过万不是梦",
    "机会来了", "抓住机会", "改变命运", "普通人的机会",
    "未来趋势", "大势所趋", "必将取代", "时代红利",
]

# 具体性信号：出现这些说明作者在讲真事，可抵消一部分空词
CONCRETE_SIGNALS: list[str] = [
    "元", "块钱", "美元", "美金", "刀",
    "客户", "顾客", "下单", "成交", "付款", "收款", "报价",
    "第一单", "第一个客户", "复购", "退款",
]

# 空词密度阈值：每千字出现多少次算「满篇空话」
HYPE_PER_1K_THRESHOLD = 3.0
# 短文本（少于该字数）单独用绝对次数判定，避免除以小分母导致密度虚高
SHORT_TEXT_LEN = 300
HYPE_ABS_THRESHOLD_SHORT = 3


def count_hype(text: str) -> int:
    """统计文本里「没有定义的空词」出现的总次数。"""
    if not text:
        return 0
    t = text.lower()
    return sum(t.count(w.lower()) for w in HYPE_WORDS)


def has_concrete_signal(text: str) -> bool:
    """文本里有没有「具体到钱和客户」的信号。有的话说明作者在讲真事。"""
    if not text:
        return False
    t = text.lower()
    return any(s.lower() in t for s in CONCRETE_SIGNALS)


def is_hype(text: str) -> bool:
    """判断一段文本是否为「满篇空词、拿不出具体东西」的噱头文。

    判定逻辑（三者同时成立才算噱头，尽量少误伤）：
      1. 空词出现次数达到密度阈值；
      2. 全文找不到任何「钱 / 客户 / 成交」这类具体信号；
      3. 文本本身不是太短（太短的标题不做判定，交给 AI）。

    对应 dbs 诊断漏斗第一层：核心词没有定义 → 这个问题本身不成立，不需要被回答。
    """
    if not text:
        return False

    n = count_hype(text)
    if n == 0:
        return False

    # 只要作者拿得出具体的钱和客户，就不算空转——他在讲真事，只是用词浮夸
    if has_concrete_signal(text):
        return False

    length = len(text)
    if length < SHORT_TEXT_LEN:
        return n >= HYPE_ABS_THRESHOLD_SHORT

    density = n / (length / 1000.0)
    return density >= HYPE_PER_1K_THRESHOLD


def hype_reason(text: str) -> str:
    """给出噱头判定的人话理由（用于日志和卡片，让判断可追溯）。"""
    if not is_hype(text):
        return ""
    t = (text or "").lower()
    hit = [w for w in HYPE_WORDS if w.lower() in t]
    return "满篇没有定义的空词（" + "、".join(hit[:4]) + "），且全文拿不出具体的钱和客户"
