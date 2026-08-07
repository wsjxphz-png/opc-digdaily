"""
Reddit 信息源 — 抓取指定的 subreddits 热门/新帖。
通过 Reddit API (免费 tier) 无需登录即可读取公开数据。
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)


class RedditSource(BaseSource):
    name = "reddit"

    # Reddit 公开 API 无需认证即可读取
    BASE = "https://www.reddit.com"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        subreddits = cfg.get("subreddits", [])
        if not subreddits:
            logger.info("Reddit: 未配置 subreddits，跳过")
            return []

        limit = cfg.get("limit", 10)
        sort = cfg.get("sort", "hot")  # hot / new / top

        items: list[ContentItem] = []
        headers = {"User-Agent": cfg.get("user_agent", "DailyOpportunityBot/1.0")}

        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            for sub in subreddits:
                try:
                    sub_items = await self._fetch_subreddit(
                        client, sub, sort, limit, keywords
                    )
                    items.extend(sub_items)
                except Exception as e:
                    logger.error(f"Reddit r/{sub} 获取失败: {e}")
                await asyncio.sleep(0.5)

        # 去重
        seen = set()
        unique = []
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)

        logger.info(f"Reddit: 获取到 {len(unique)} 条")
        return unique

    async def _fetch_subreddit(
        self, client: httpx.AsyncClient, sub: str,
        sort: str, limit: int, keywords: list[str],
    ) -> list[ContentItem]:
        url = f"{self.BASE}/r/{sub}/{sort}.json"
        params = {"limit": limit, "raw_json": 1}
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        items = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            # 跳过置顶帖和广告
            if post.get("stickied") or post.get("is_self", False) and not post.get("selftext"):
                pass

            title = post.get("title", "")
            selftext = post.get("selftext", "")[:300]
            permalink = post.get("permalink", "")
            subreddit = post.get("subreddit_name_prefixed", "")
            created_utc = post.get("created_utc", 0)
            url_post = post.get("url", "")
            ups = post.get("ups", 0)
            num_comments = post.get("num_comments", 0)
            thumbnail = post.get("thumbnail", "")

            if thumbnail in ("self", "default", "nsfw", ""):
                thumbnail = ""

            pub_dt = None
            if created_utc:
                pub_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)

            full_text = f"{title}\n{selftext}"
            score = self.keyword_score(full_text, keywords)

            # Reddit self post 用 permalink，link post 用真实 url
            final_url = f"https://www.reddit.com{permalink}"
            if not post.get("is_self"):
                final_url = url_post if url_post else final_url

            items.append(ContentItem(
                title=title,
                url=final_url,
                summary=f"[r/{sub} | {ups} upvotes | {num_comments} comments]\n{selftext}",
                source="reddit",
                source_name=subreddit,
                published=pub_dt,
                thumbnail=thumbnail,
                relevance_score=score * (1 + min(ups / 500, 1) * 0.3),  # 热门帖加权
            ))
        return items
