"""
RSSHub 桥接源 — 通过公开 RSSHub 实例抓取中文平台内容。

零代码方案：使用社区维护的公开 RSSHub 节点，不需要自己部署 Docker。
支持多镜像自动回退：当一个节点不可用时，自动尝试下一个。
"""
import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

# 公开 RSSHub 节点列表（按优先级排序，自动回退）
# 不需要自己部署 Docker，直接使用社区维护的公开实例
DEFAULT_MIRRORS = [
    "https://rsshub.rssforever.com",
]


class RSSHubSource(BaseSource):
    """RSSHub 桥接内容源。通过公开实例抓取中文平台内容。"""

    name = "rsshub"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("RSSHub: 未启用，跳过")
            return []

        # 支持多镜像配置（mirrors 列表），也兼容旧版单 mirror 字段
        mirrors = cfg.get("mirrors") or [cfg.get("mirror", DEFAULT_MIRRORS[0])]
        if isinstance(mirrors, str):
            mirrors = [mirrors]
        if not mirrors:
            mirrors = DEFAULT_MIRRORS

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

                # 多镜像回退：逐个尝试直到成功
                success = False
                for mirror in mirrors:
                    url = f"{mirror.rstrip('/')}{route}"
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            success = True
                            break
                        elif resp.status_code == 403:
                            logger.debug(f"RSSHub [{label}]: {mirror} 返回 403（可能被墙），尝试下一个镜像")
                        else:
                            logger.warning(f"RSSHub [{label}]: {mirror} HTTP {resp.status_code}")
                    except asyncio.TimeoutError:
                        logger.debug(f"RSSHub [{label}]: {mirror} 超时，尝试下一个镜像")
                    except Exception as e:
                        logger.debug(f"RSSHub [{label}]: {mirror} {type(e).__name__}")

                if not success:
                    logger.warning(f"RSSHub [{label}]: 所有镜像均不可用，跳过")
                    continue

                try:
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
                    logger.warning(f"RSSHub [{label}]: 解析超时，跳过")
                except Exception as e:
                    logger.error(f"RSSHub [{label}]: {type(e).__name__}: {e}")

        logger.info(f"RSSHub: 共获取 {len(items)} 条")
        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", "", text)
