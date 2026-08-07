"""
YouTube 信息源 — 支持 Data API v3（需 API key）和 RSS 模式（免费，不需 key）。
RSS 模式通过 https://www.youtube.com/feeds/videos.xml?channel_id=XXX 抓取频道最近视频。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
import feedparser

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


class YouTubeSource(BaseSource):
    name = "youtube"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        api_key = cfg.get("api_key", "")
        use_api = bool(api_key and not api_key.startswith("YOUR_"))
        use_rss = cfg.get("rss_feeds", {}).get("enabled", True)

        if not use_api and not use_rss:
            logger.warning("YouTube: 无 API key 且 RSS 未启用，跳过")
            return []

        items: list[ContentItem] = []
        seen_urls: set[str] = set()

        if use_api:
            items = await self._fetch_api(cfg, keywords, seen_urls)
        elif use_rss:
            items = await self._fetch_rss(cfg, keywords, seen_urls)

        logger.info(f"YouTube: 获取到 {len(items)} 条")
        return items

    # ============================================================
    # RSS 模式（免费，无需 API key）
    # ============================================================

    async def _fetch_rss(self, cfg: dict, keywords: dict, seen: set[str]) -> list[ContentItem]:
        rss_cfg = cfg.get("rss_feeds", {})
        channel_ids = rss_cfg.get("channel_ids", [])

        if not channel_ids:
            logger.info("YouTube RSS: 无频道配置，跳过")
            return []

        limit = rss_cfg.get("limit", 5)
        items: list[ContentItem] = []
        loop = asyncio.get_event_loop()

        for ch_id in channel_ids:
            try:
                label = ch_id.get("label", ch_id.get("id", "Unknown"))
                cid = ch_id.get("id", "")
                if not cid:
                    continue

                url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
                raw = await loop.run_in_executor(None, feedparser.parse, url)

                for entry in raw.entries[:limit]:
                    title = entry.get("title", "")
                    link = entry.get("link", "")

                    # YouTube RSS description 包含视频描述，较长
                    desc = entry.get("description", entry.get("summary", ""))
                    desc = self._strip_html(desc)[:500]

                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        from time import mktime
                        ts = mktime(entry.published_parsed)
                        published = datetime.fromtimestamp(ts, tz=timezone.utc)

                    # 只取最近 3 天的
                    if published and published < datetime.now(timezone.utc) - timedelta(days=3):
                        continue

                    full_text = f"{title}\n{desc}"
                    score = self.keyword_score(full_text, keywords)

                    vid = link.split("v=")[-1] if "v=" in link else ""
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""

                    items.append(ContentItem(
                        title=title,
                        url=link,
                        summary=desc[:300],
                        source="youtube",
                        source_name=label,
                        published=published,
                        thumbnail=thumb,
                        relevance_score=score,
                    ))
            except Exception as e:
                logger.error(f"YouTube RSS {label} 失败: {e}")

        return items

    # ============================================================
    # API 模式（需要 API key）
    # ============================================================

    async def _fetch_api(self, cfg: dict, keywords: dict, seen: set[str]) -> list[ContentItem]:
        api_key = cfg.get("api_key", "")
        items: list[ContentItem] = []

        async with httpx.AsyncClient(timeout=15) as client:
            for channel_id in cfg.get("channels", []):
                try:
                    channel_items = await self._fetch_channel(client, api_key, channel_id, keywords)
                    for it in channel_items:
                        if it.url not in seen:
                            seen.add(it.url)
                            items.append(it)
                except Exception as e:
                    logger.error(f"YouTube 频道 {channel_id} 获取失败: {e}")

            for query in cfg.get("search_queries", []):
                try:
                    search_items = await self._search(
                        client, api_key, query, keywords, max_results=cfg.get("max_results", 5)
                    )
                    for it in search_items:
                        if it.url not in seen:
                            seen.add(it.url)
                            items.append(it)
                except Exception as e:
                    logger.error(f"YouTube 搜索 '{query}' 失败: {e}")

            await asyncio.sleep(0.1)

        return items

    async def _fetch_channel(self, client, api_key, channel_id, keywords) -> list[ContentItem]:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": api_key,
            "channelId": channel_id,
            "part": "snippet",
            "order": "date",
            "maxResults": 10,
            "type": "video",
            "publishedAfter": (datetime.now(CST) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"),
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
                title=title, url=f"https://www.youtube.com/watch?v={video_id}",
                summary=description[:300], source="youtube", source_name=channel_title,
                published=pub_dt, thumbnail=thumb_url, relevance_score=score,
            ))
        return items

    async def _search(self, client, api_key, query, keywords, max_results=5) -> list[ContentItem]:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": api_key, "q": query, "part": "snippet", "order": "date",
            "maxResults": max_results, "type": "video", "relevanceLanguage": "en",
            "publishedAfter": (datetime.now(CST) - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z"),
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
                title=title, url=f"https://www.youtube.com/watch?v={video_id}",
                summary=description[:300], source="youtube", source_name=channel_title,
                published=pub_dt, thumbnail=thumb_url, relevance_score=score,
            ))
        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", text).strip()
