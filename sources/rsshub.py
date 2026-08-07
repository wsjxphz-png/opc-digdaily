"""
RSSHub 桥接源 — 通过 RSSHub 实例抓取中文平台内容。

支持知乎、V2EX、即刻、B站等平台，通过 RSSHub mirror 桥接为 RSS。
"""
import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)


class RSSHubSource(BaseSource):
    """RSSHub 桥接内容源。通过 mirror 实例抓取中文平台内容。"""

    name = "rsshub"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("RSSHub: 未启用，跳过")
            return []

        mirror = cfg.get("mirror", "https://rsshub.rssforever.com")
        timeout = cfg.get("timeout", 30)
        routes = cfg.get("routes", [])

        items: list[ContentItem] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            for route_def in routes:
                route = route_def.get("route", "")
                label = route_def.get("label", route)
                url = f"{mirror.rstrip('/')}{route}"

                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"RSSHub [{label}]: HTTP {resp.status_code}")
                        continue

                    f = feedparser.parse(resp.text)
                    entries = f.entries[:10]

                    for entry in entries:
                        title = entry.get("title", "")
                        link = entry.get("link", "")
                        summary = entry.get("summary", entry.get("description", ""))
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

                        items.append(ContentItem(
                            title=title,
                            url=link,
                            summary=summary,
                            source="rsshub",
                            source_name=label,
                            published=published,
                        ))

                    logger.info(f"RSSHub [{label}]: {len(entries)} 条")

                except asyncio.TimeoutError:
                    logger.warning(f"RSSHub [{label}]: 超时，跳过")
                except Exception as e:
                    logger.error(f"RSSHub [{label}]: {type(e).__name__}: {e}")

        logger.info(f"RSSHub: 共获取 {len(items)} 条")
        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", "", text)
