"""
RSS 信息源 — 支持任意 RSS/Atom 订阅源，包括 RSSHub 桥接。
"""

import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)


class RSSSource(BaseSource):
    name = "rss"

    async def fetch(self, cfg: dict, keywords: list[str]) -> list[ContentItem]:
        feeds = cfg.get("feeds", [])
        if not feeds:
            logger.info("RSS: 无配置源，跳过")
            return []

        items: list[ContentItem] = []
        loop = asyncio.get_event_loop()

        for feed_def in feeds:
            name = feed_def.get("name", "Unknown")
            url = feed_def.get("url", "")
            if not url:
                continue

            try:
                # feedparser 是同步的，放线程池跑
                raw = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in raw.entries[:10]:
                    title = entry.get("title", "")
                    link = entry.get("link", "")
                    summary = entry.get("summary", entry.get("description", ""))
                    # 清理 HTML 标签
                    summary = self._strip_html(summary)[:300]

                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        from time import mktime
                        ts = mktime(entry.published_parsed)
                        published = datetime.fromtimestamp(ts, tz=timezone.utc)
                    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                        from time import mktime
                        ts = mktime(entry.updated_parsed)
                        published = datetime.fromtimestamp(ts, tz=timezone.utc)

                    full_text = f"{title}\n{summary}"
                    score = self.keyword_score(full_text, keywords)

                    items.append(ContentItem(
                        title=title,
                        url=link,
                        summary=summary,
                        source="rss",
                        source_name=name,
                        published=published,
                        relevance_score=score,
                    ))
            except Exception as e:
                logger.error(f"RSS {name} ({url}) 获取失败: {e}")

        logger.info(f"RSS: 获取到 {len(items)} 条")
        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        """简单去除 HTML 标签。"""
        import re
        return re.sub(r"<[^>]+>", " ", text).strip()
