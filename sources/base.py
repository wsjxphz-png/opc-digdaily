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
    source: str = ""           # 源标识: youtube / rss / reddit / twitter / wechat
    source_name: str = ""      # 频道名/博客名/用户等
    published: Optional[datetime] = None
    thumbnail: str = ""        # 封面图URL

    # AI 处理后填充
    translation: str = ""      # 中文翻译
    ai_summary: str = ""       # AI 精简总结
    opportunity_hint: str = "" # 赚钱机会提示
    relevance_score: float = 0.0

    def dict_key(self) -> str:
        """用于去重，基于 URL。"""
        return self.url


class BaseSource(ABC):
    """信息源抽象基类。"""

    name: str = "base"

    @abstractmethod
    async def fetch(self, cfg: dict, keywords: list[str]) -> list[ContentItem]:
        """抓取内容，返回 ContentItem 列表。"""
        ...

    def keyword_score(self, text: str, keywords: list[str]) -> float:
        """计算文本与关键词的匹配度 (0~1)。"""
        if not text or not keywords:
            return 0.0
        text_lower = text.lower()
        hits = sum(1 for kw in keywords if kw.lower() in text_lower)
        return min(hits / max(len(keywords), 1), 1.0)
