"""
Reddit 信息源 — 通过 Reddit 内置 RSS 抓取热门/新帖。
RSS 方式比 JSON API 更稳定，不受 User-Agent 限制。
"""

import asyncio
import logging
from datetime import datetime, timezone

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)


class RedditSource(BaseSource):
    name = "reddit"

    BASE_RSS = "https://www.reddit.com/r/{sub}/.rss"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        subreddits = cfg.get("subreddits", [])
        if not subreddits:
            logger.info("Reddit: 未配置 subreddits，跳过")
            return []

        limit = cfg.get("limit", 10)

        items: list[ContentItem] = []
        # 按批次并发抓取，每批最多 5 个，避免被限
        batch_size = 5
        for i in range(0, len(subreddits), batch_size):
            batch = subreddits[i:i + batch_size]
            tasks = [self._fetch_rss(sub, limit, keywords) for sub in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for sub, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.error(f"Reddit r/{sub} 获取失败: {result}")
                else:
                    items.extend(result)

            if i + batch_size < len(subreddits):
                await asyncio.sleep(2)  # 批次间暂停

        # 去重
        seen = set()
        unique = []
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)

        logger.info(f"Reddit: 获取到 {len(unique)} 条")
        return unique

    async def _fetch_rss(self, sub: str, limit: int, keywords: dict) -> list[ContentItem]:
        """通过 Reddit RSS 抓取。"""
        import feedparser
        loop = asyncio.get_event_loop()

        url = self.BASE_RSS.format(sub=sub)
        raw = await loop.run_in_executor(None, feedparser.parse, url)

        items = []
        for entry in raw.entries[:limit]:
            title = entry.get("title", "")
            link = entry.get("link", "")
            summary = entry.get("summary", entry.get("description", ""))
            summary = self._strip_html(summary)[:300]

            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                from time import mktime
                ts = mktime(entry.published_parsed)
                published = datetime.fromtimestamp(ts, tz=timezone.utc)

            full_text = f"{title}\n{summary}"
            score = self.keyword_score(full_text, keywords)

            items.append(ContentItem(
                title=title,
                url=link,
                summary=f"[r/{sub}]\n{summary}",
                source="reddit",
                source_name=f"r/{sub}",
                published=published,
                relevance_score=score,
            ))
        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", text).strip()
