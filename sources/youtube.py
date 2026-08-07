"""
YouTube 信息源 — 通过 Data API v3 搜索或订阅频道获取视频。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

# 北京时间时区
CST = timezone(timedelta(hours=8))


class YouTubeSource(BaseSource):
    name = "youtube"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        api_key = cfg.get("api_key", "")
        if not api_key or api_key.startswith("YOUR_"):
            logger.warning("YouTube API key 未配置，跳过")
            return []

        items: list[ContentItem] = []
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(timeout=15) as client:
            # 方式1：从频道获取
            for channel_id in cfg.get("channels", []):
                try:
                    channel_items = await self._fetch_channel(
                        client, api_key, channel_id, keywords
                    )
                    for it in channel_items:
                        if it.url not in seen_urls:
                            seen_urls.add(it.url)
                            items.append(it)
                except Exception as e:
                    logger.error(f"YouTube 频道 {channel_id} 获取失败: {e}")

            # 方式2：搜索
            for query in cfg.get("search_queries", []):
                try:
                    search_items = await self._search(
                        client, api_key, query, keywords,
                        max_results=cfg.get("max_results", 5)
                    )
                    for it in search_items:
                        if it.url not in seen_urls:
                            seen_urls.add(it.url)
                            items.append(it)
                except Exception as e:
                    logger.error(f"YouTube 搜索 '{query}' 失败: {e}")

            # 等待一会儿避免请求过密
            await asyncio.sleep(0.1)

        logger.info(f"YouTube: 获取到 {len(items)} 条")
        return items

    async def _fetch_channel(
        self, client: httpx.AsyncClient, api_key: str,
        channel_id: str, keywords: list[str]
    ) -> list[ContentItem]:
        """获取频道最近视频。"""
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": api_key,
            "channelId": channel_id,
            "part": "snippet",
            "order": "date",
            "maxResults": 10,
            "type": "video",
            "publishedAfter": (datetime.now(CST) - timedelta(days=2)).strftime(
                "%Y-%m-%dT00:00:00Z"
            ),
        }
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        items = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            if not video_id:
                continue
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            thumbnails = snippet.get("thumbnails", {})
            thumb_url = thumbnails.get("high", thumbnails.get("default", {})).get("url", "")
            channel_title = snippet.get("channelTitle", "")
            published = snippet.get("publishedAt")

            pub_dt = None
            if published:
                try:
                    pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    pass

            full_text = f"{title}\n{description}"
            score = self.keyword_score(full_text, keywords)

            items.append(ContentItem(
                title=title,
                url=f"https://www.youtube.com/watch?v={video_id}",
                summary=description[:300],
                source="youtube",
                source_name=channel_title,
                published=pub_dt,
                thumbnail=thumb_url,
                relevance_score=score,
            ))
        return items

    async def _search(
        self, client: httpx.AsyncClient, api_key: str,
        query: str, keywords: list[str], max_results: int = 5,
    ) -> list[ContentItem]:
        """搜索视频。"""
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": api_key,
            "q": query,
            "part": "snippet",
            "order": "date",
            "maxResults": max_results,
            "type": "video",
            "relevanceLanguage": "en",
            "publishedAfter": (datetime.now(CST) - timedelta(days=3)).strftime(
                "%Y-%m-%dT00:00:00Z"
            ),
        }
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        items = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            if not video_id:
                continue
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            thumbnails = snippet.get("thumbnails", {})
            thumb_url = thumbnails.get("high", thumbnails.get("default", {})).get("url", "")
            channel_title = snippet.get("channelTitle", "")
            published = snippet.get("publishedAt")

            pub_dt = None
            if published:
                try:
                    pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    pass

            full_text = f"{title}\n{description}"
            score = self.keyword_score(full_text, keywords)

            items.append(ContentItem(
                title=title,
                url=f"https://www.youtube.com/watch?v={video_id}",
                summary=description[:300],
                source="youtube",
                source_name=channel_title,
                published=pub_dt,
                thumbnail=thumb_url,
                relevance_score=score,
            ))
        return items
