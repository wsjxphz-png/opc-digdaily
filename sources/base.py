"""
信息源基类 — 统一接口，每个源实现 fetch() 方法返回标准化条目列表。
"""

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
    difficulty: str = ""       # 门槛: 零门槛 / 需学习 / 有一定门槛
    quality_flag: str = ""      # AI 质量标记: ⭐ / "" / ⚠️
    relevance_score: float = 0.0

    # 严苛商业分析师评估维度
    code_dependency: int = 0       # 代码依赖度 1-5（5=必须精通编程）
    authenticity: int = 0          # 真实性打分 1-5（1=纯卖课/卖铲子）
    practical_steps: str = ""      # 核心实操步骤拆解（去除废话后）
    verdict: str = ""              # 结论: "可复刻的真机会" / "卖噱头/卖铲子"

    # 客观打分（scoring.py 用固定公式算出，AI 只提供 1-5 的事实型子因子）
    score_factors: dict = field(default_factory=dict)  # 12 个子因子原始分
    commercial_score: int = 0      # 商业化潜力 0-100
    feasibility_score: int = 0     # 可行性 0-100
    startup_index: int = 0         # 适合你启动的指数 1-10
    score_reason: str = ""         # 分数怎么来的（加分项/扣分项/封顶原因）

    # 可抄模板（照着做的最小行动包）
    copy_template: dict = field(default_factory=dict)

    # 跨天机会库标注（library.py 填充）
    topic_key: str = ""            # 归并同一主题用的指纹
    repeat_count: int = 0          # 这个主题历史上第几次出现
    corroborations: int = 0        # 被多少个不同来源印证过
    first_seen: str = ""           # 首次出现日期 YYYY-MM-DD

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
        """
        计算文本与关键词的匹配度 (0~1)。
        
        两级关键词：
        - strong: 必须至少命中 1 个才能过关（OPC 专属词）
        - weak: 命中越多得分越高
        """
        if not text or not keywords:
            return 0.0

        text_lower = text.lower()
        strong = keywords.get("strong", [])
        weak = keywords.get("weak", [])

        # 必须命中至少 1 个强关键词
        strong_hits = sum(1 for kw in strong if kw.lower() in text_lower)
        if strong_hits == 0:
            return 0.0  # 直接淘汰

        # 弱关键词加分
        weak_hits = sum(1 for kw in weak if kw.lower() in text_lower)

        # 基础分 0.2（命中强关键词），弱关键词每命中 1 个 +0.05，上限 0.6
        base = 0.2
        bonus = min(weak_hits * 0.05, 0.4)
        return min(base + bonus, 0.6)


def has_strong_keyword(text: str, keywords: dict) -> bool:
    """检查文本是否命中至少 1 个强关键词。"""
    if not text or not keywords:
        return False
    text_lower = text.lower()
    strong = keywords.get("strong", [])
    return any(kw.lower() in text_lower for kw in strong)
