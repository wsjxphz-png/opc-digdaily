"""
X (Twitter) 信息源 — 双通道：Nitter RSS (免费) + Twitter API (付费)。

推荐使用 Nitter RSS 方案，完全免费无需 API Key。
账号列表在 config.yaml 的 twitter.nitter_rss.accounts 配置。
"""

import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)


class TwitterSource(BaseSource):
    name = "twitter"

    async def fetch(self, cfg: dict, keywords: list[str]) -> list[ContentItem]:
        items: list[ContentItem] = []

        # 优先用 Nitter RSS (免费)
        nitter = cfg.get("nitter_rss", {})
        if nitter.get("enabled"):
            items.extend(await self._fetch_nitter(nitter, keywords))

        # 如果有 Twitter API Bearer Token 也试试
        bearer = cfg.get("bearer_token", "")
        if bearer and not bearer.startswith("YOUR_"):
            items.extend(await self._fetch_api(cfg, bearer, keywords))

        logger.info(f"Twitter: 获取到 {len(items)} 条")
        return items

    async def _fetch_nitter(
        self, nitter: dict, keywords: list[str]
    ) -> list[ContentItem]:
        """通过 Nitter 实例的 RSS feed 获取推文。"""
        instance = nitter.get("instance", "https://nitter.net").rstrip("/")
        accounts = nitter.get("accounts", [])
        items: list[ContentItem] = []
        loop = asyncio.get_event_loop()

        for account in accounts:
            rss_url = f"{instance}/{account}/rss"
            try:
                raw = await loop.run_in_executor(None, feedparser.parse, rss_url)
                for entry in raw.entries[:5]:
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

                    # Nitter RSS 推文 title 含 @handle
                    items.append(ContentItem(
                        title=title,
                        url=link,
                        summary=summary,
                        source="twitter",
                        source_name=f"@{account}",
                        published=published,
                        relevance_score=score,
                    ))
            except Exception as e:
                logger.error(f"Nitter RSS @{account} 获取失败: {e}")

        return items

    async def _fetch_api(
        self, cfg: dict, bearer_token: str, keywords: list[str]
    ) -> list[ContentItem]:
        """通过 Twitter API v2 获取推文 (需要付费 tier)。"""
        accounts = cfg.get("accounts", [])
        if not accounts:
            return []

        items: list[ContentItem] = []
        headers = {"Authorization": f"Bearer {bearer_token}"}

        async with httpx.AsyncClient(timeout=15) as client:
            for account in accounts:
                try:
                    # 获取用户 ID
                    user_url = f"https://api.twitter.com/2/users/by/username/{account}"
                    user_resp = await client.get(user_url, headers=headers)
                    user_resp.raise_for_status()
                    user_data = user_resp.json()
                    user_id = user_data.get("data", {}).get("id")
                    if not user_id:
                        continue

                    # 获取最近推文
                    tweet_url = f"https://api.twitter.com/2/users/{user_id}/tweets"
                    params = {
                        "max_results": 5,
                        "tweet.fields": "created_at,public_metrics",
                    }
                    tweet_resp = await client.get(
                        tweet_url, headers=headers, params=params
                    )
                    tweet_resp.raise_for_status()
                    tweet_data = tweet_resp.json()

                    for tweet in tweet_data.get("data", []):
                        text = tweet.get("text", "")
                        tw_id = tweet.get("id", "")
                        created = tweet.get("created_at", "")

                        pub_dt = None
                        if created:
                            try:
                                pub_dt = datetime.fromisoformat(
                                    created.replace("Z", "+00:00")
                                )
                            except ValueError:
                                pass

                        score = self.keyword_score(text, keywords)
                        items.append(ContentItem(
                            title=text[:150],
                            url=f"https://x.com/{account}/status/{tw_id}",
                            summary=text[:300],
                            source="twitter",
                            source_name=f"@{account}",
                            published=pub_dt,
                            relevance_score=score,
                        ))
                except Exception as e:
                    logger.error(f"Twitter API @{account} 失败: {e}")

        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", text).strip()
