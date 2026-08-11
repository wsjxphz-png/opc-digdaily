"""
信息源基类 — 统一接口，每个源实现 fetch() 方法返回标准化条目列表。
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ContentItem:
    """标准化内容条目，所有信息源统一输出此格式。"""
    title: str
    url: str
    summary: str = ""          # 原始摘要/描述
    full_text: str = ""         # 全文内容（enricher 填充，用于 AI 深度判断）
    source: str = ""           # 源标识: youtube / rss / reddit / twitter / wechat
    source_name: str = ""      # 频道名/博客名/用户等
    published: Optional[datetime] = None
    thumbnail: str = ""        # 封面图URL

    # AI 处理后填充
    translation: str = ""      # 中文翻译
    ai_summary: str = ""       # AI 精简总结
    opportunity_hint: str = "" # 赚钱机会提示
    xhs_title: str = ""        # 小红书标题（dbs-xhs-title 公式生成，≤20字，可直接发）
    difficulty: str = ""       # 门槛: 零门槛 / 需学习 / 有一定门槛
    quality_flag: str = ""      # AI 质量标记: ⭐ / "" / ⚠️
    relevance_score: float = 0.0

    # 严苛商业分析师评估维度
    code_dependency: int = 0       # 代码依赖度 1-5（5=必须精通编程）
    authenticity: int = 0          # 真实性打分 1-5（1=纯卖课/卖铲子）
    practical_steps: str = ""      # 核心实操步骤拆解（去除废话后）
    verdict: str = ""              # 结论: "可复刻的真机会" / "卖噱头/卖铲子"

    # 客观打分（scoring.py 用固定公式算出，AI 只提供 1-5 的事实型子因子）
    score_factors: dict = field(default_factory=dict)  # 全部子因子原始分
    commercial_score: int = 0      # 商业化潜力 0-100
    feasibility_score: int = 0     # 可行性 0-100
    startup_index: int = 0         # 适合你启动的指数 1-10
    score_reason: str = ""         # 分数怎么来的（加分项/扣分项/封顶原因）
    gate_reason: str = ""          # dbs 七项检验里没通过的项（人话）
    hype_flag: bool = False        # 确定性关键词判定：满篇空词的噱头文

    # 可抄模板（照着做的最小行动包）
    copy_template: dict = field(default_factory=dict)

    # 跨天机会库标注（library.py 填充）
    topic_key: str = ""            # 归并同一主题用的指纹
    repeat_count: int = 0          # 这个主题历史上第几次出现
    corroborations: int = 0        # 被多少个不同来源印证过
    first_seen: str = ""           # 首次出现日期 YYYY-MM-DD

    # 竞争热度软信号（用户 2026-08-11）：高频≠硬杀，只降权+标注
    competition_heat: int = 0       # 0=低 1=升温 2=红海
    red_ocean: bool = False         # 高频红海标记：仅降权+标注，不硬杀
    heat_penalty: int = 0           # 红海降权分值（从启动指数扣，仅参与排序）

    ai_processed: bool = False # 是否经过 AI 处理

    def dict_key(self) -> str:
        """用于去重，基于 URL。"""
        return self.url


class BaseSource(ABC):
    """信息源抽象基类。"""

    name: str = "base"

    @abstractmethod
    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        """抓取内容，返回 ContentItem 列表。"""
        ...

    def keyword_score(self, text: str, keywords: dict) -> float:
        """文本与关键词匹配度（0~1），实现见模块级 _keyword_score_fn。

        注意：必须是 BaseSource 的方法，子类（RSSSource/RedditSource/TwitterSource/
        YouTubeSource）都通过 self.keyword_score(...) 调用。曾因误改成模块级函数
        导致所有源采集时 AttributeError、RSS 全线挂掉。
        """
        return _keyword_score_fn(text, keywords)

# ============================================================
# 关键词匹配：避免短英文词裸子串误命中 + 素人/真实生意叙事旁路
# ============================================================
#
# 死板教条问题（用户原话：「过滤器只会死板地找关键词，不看上下文」）：
#   1) 英文短词裸子串误命中 —— 'merch' 命中 'merchant banking'、'royalty'
#      命中 'Royalty payments in oil and gas'。纯字母英文词加词边界即可避免。
#   2) 强关键词闸门要求命中「一人公司 / 副业 / 月入」这类行话才放行，但大量
#      最该推的素人真实赚钱故事是用大白话写的（「宝妈手工皂月售3000」「夫妻
#      遛狗上门服务」「被裁后做小红书博主」），一个行话都没有，被死板枪毙在
#      AI 之前。修法：命中「人物身份 + 生意/平台动作」或「具体金额 + 生意动词」
#      组合，即使没有行话强词也放行进 AI（真正的判断交给 AI + dbs 硬闸）。

def _kw_match(kw: str, text_lower: str) -> bool:
    """关键词命中：纯 ASCII 短词用词边界，避免 merch→merchant 这类误命中。

    边界要求关键词前后都不是「字母/数字/撇号 s」——这样 'royalty' 不会命中
    'royalties'（royalties 在 y 之后还有 ies），但 'royalty' 仍能命中 'royalty'、
    'royalty payments'、'earn royalty' 等正常用法。
    """
    k = kw.lower()
    if k.isascii() and any(c.isalpha() for c in k):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9's])", text_lower))
    return k in text_lower


# 素人 / 真实小生意叙事旁路：同时出现「人物身份」+「生意/平台动作」即放行
PERSON_CUES = [
    "宝妈", "宝爸", "夫妻", "她", "他", "普通人", "素人", "被裁", "裁员", "失业",
    "辞职", "离职", "退休", "大学生", "应届", "上班族", "白领", "主妇", "农村",
    "小镇", "大爷", "大妈", "爷爷", "奶奶", "阿姨", "大叔", "小伙", "姑娘",
    "单亲", "退役", "护士", "教师", "银发",
]
BIZ_CUES = [
    "手工", "摆摊", "市集", "上门", "代运营", "代账", "陪练", "花艺", "教室",
    "工作室", "副业", "自媒体", "博主", "小红书", "抖音", "视频号", "公众号",
    "播客", "淘宝", "闲鱼", "电商", "开课", "训练营", "社群", "私域", "接单",
    "接活", "卖", "售", "赚", "月入", "收入", "收费", "定价", "客单价",
    "老手艺", "手作", "烘焙", "收纳", "遛狗", "宠物", "家政", "保洁", "家教",
    "咨询", "服务", "开店", "直播", "带货",
]
_EN_PERSON = (
    r"\b(mom|mother|mum|dad|father|couple|retired|laid off|fired|stay-at-home|"
    r"student|teacher|nurse|she|he|grandma|grandpa|regular guy|ordinary person)\b"
)
_EN_BIZ = (
    r"\b(selling|sell|side hustle|etsy|shopify|patreon|onlyfans|substack|youtube|"
    r"tiktok|instagram|freelance|consult|coach|course|printable|crochet|handmade|"
    r"craft|tutor|rental|affiliate|dropship|notion template|make money|earn|income|"
    r"passive|revenue)\b"
)
# 金额数字（含货币单位）：用于「具体金额 + 生意动词」旁路
_MONEY_RE = re.compile(r"\d[\d.,]*\s*(?:元|块|刀|美元|美金|万|千|k|m|w|\$|€|£)")
_BIZ_VERB = [
    "卖", "售", "赚", "收费", "月入", "收入", "客单价", "接单", "定价", "营收",
    "利润", "sell", "selling", "make", "earn", "earning", "income", "charge",
    "revenue", "monthly", "per month",
]


def _person_business_bypass(t: str) -> bool:
    """素人身份 + 生意/平台动作 → 放行进 AI 评估。"""
    if any(p in t for p in PERSON_CUES) and any(b in t for b in BIZ_CUES):
        return True
    en_person = re.search(_EN_PERSON, t) is not None
    en_biz = re.search(_EN_BIZ, t) is not None
    return bool(en_person and en_biz)


def _money_story_bypass(t: str) -> bool:
    """具体金额 + 生意动词 → 放行进 AI（如 'make 4k a month selling crochet'）。"""
    if not _MONEY_RE.search(t):
        return False
    return any(v in t for v in _BIZ_VERB)


def _keyword_score_fn(text: str, keywords: dict) -> float:
    """
    计算文本与关键词的匹配度 (0~1)。

    两级关键词：
    - strong: 命中至少 1 个才过关（OPC 专属词）；无强词但有素人/金额旁路也放行
    - weak: 命中越多得分越高
    """
    if not text or not keywords:
        return 0.0

    text_lower = text.lower()
    strong = keywords.get("strong", [])
    weak = keywords.get("weak", [])

    # 命中至少 1 个强关键词，或触发素人/金额旁路
    strong_hits = sum(1 for kw in strong if _kw_match(kw, text_lower))
    if strong_hits == 0 and not (
        _person_business_bypass(text_lower) or _money_story_bypass(text_lower)
    ):
        return 0.0  # 直接淘汰

    # 弱关键词加分
    weak_hits = sum(1 for kw in weak if _kw_match(kw, text_lower))

    # 基础分 0.2（命中强关键词/旁路），弱关键词每命中 1 个 +0.05，上限 0.6
    base = 0.2
    bonus = min(weak_hits * 0.05, 0.4)
    return min(base + bonus, 0.6)


def has_strong_keyword(text: str, keywords: dict) -> bool:
    """检查文本是否命中至少 1 个强关键词，或触发素人/金额旁路放行。"""
    if not text or not keywords:
        return False
    t = text.lower()
    strong = keywords.get("strong", [])
    if any(_kw_match(kw, t) for kw in strong):
        return True
    # 旁路：素人/真实小生意叙事，即使没有 OPC 行话强词也放行进 AI 评估
    return _person_business_bypass(t) or _money_story_bypass(t)
