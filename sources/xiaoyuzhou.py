"""
小宇宙（中文播客）源 — 免登录抓取「一人公司 / 副业 / 自由职业」主题播客更新。

设计要点：
- 小宇宙播客遵循标准 podcast RSS，且大多在 Apple Podcasts 上架。
- 用 iTunes Search API（media=podcast）按主题关键词搜索，拿到 feedUrl（多为
  feed.xyzfm.space/... 小宇宙 CDN 或喜马拉雅/soundon 等托管）。
- 用浏览器 UA 抓 RSS（python-httpx 默认 UA 会被服务器返回 0 字节，必须带 Chrome UA）。
- 每个播客取最新若干集，作为「发现新 IP / 新一人公司」信号喂给模块1发现引擎，
  也一并进入模块2 机会评估（播客偏访谈讨论，大部分会被硬过滤，属正常）。
- 因为是「按主题订阅」而非「全量采集」，预筛阶段对 xiaoyuzhou 来源豁免二次关键词过滤
  （已在 main.py _collect 里处理）。
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

ITUNES = "https://itunes.apple.com/search"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_QUERIES = ["一人公司", "副业", "自由职业", "个人IP", "个体经营", "不上班", "小本创业"]


def _clean(t: str) -> str:
    """去 CDATA / HTML 标签 / 常见实体，得到干净文本。"""
    if not t:
        return ""
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&ldquo;", "“"), ("&rdquo;", "”"), ("&hellip;", "…"), ("&middot;", "·"),
    ]:
        t = t.replace(a, b)
    return t.strip()


class XiaoyuzhouSource(BaseSource):
    """小宇宙播客源：iTunes 搜主题 → RSS 抓最新单集。"""

    name = "xiaoyuzhou"

    async def fetch(self, cfg: dict, keywords: dict) -> list:
        if not cfg.get("enabled", False):
            return []
        queries = cfg.get("queries", DEFAULT_QUERIES)
        max_feeds = cfg.get("max_feeds_per_query", 3)
        max_eps = cfg.get("max_episodes", 2)
        delay = cfg.get("query_delay", 2)

        # ── Phase 1: 关键词搜播客，收集 feedUrl ──
        feeds: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            for i, q in enumerate(queries):
                if i > 0 and delay:
                    await asyncio.sleep(delay)
                try:
                    r = await c.get(
                        ITUNES,
                        params={"media": "podcast", "term": q,
                                "limit": max_feeds * 3, "country": "CN"},
                    )
                    data = r.json()
                    for e in data.get("results", []):
                        feed = e.get("feedUrl")
                        name = e.get("collectionName", "")
                        if feed and feed not in feeds:
                            feeds[feed] = name
                except Exception as ex:
                    logger.error(f"小宇宙 iTunes 搜 '{q}': {ex}")

        # ── Phase 2: 抓每个 feed 最新 N 集 ──
        items: list[ContentItem] = []
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=True,
            headers={"User-Agent": UA,
                      "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        ) as c:
            for feed, pname in feeds.items():
                try:
                    r = await c.get(feed)
                    if r.status_code != 200 or not r.text:
                        continue
                    blocks = re.findall(r"<item[ >].*?</item>", r.text, re.DOTALL)
                    parsed = []
                    for b in blocks[:60]:
                        tm = re.search(
                            r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", b, re.DOTALL)
                        dm = re.search(
                            r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
                            b, re.DOTALL)
                        lm = re.search(
                            r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", b, re.DOTALL)
                        pm = re.search(r"<pubDate>(.*?)</pubDate>", b)
                        title = _clean(tm.group(1)) if tm else ""
                        desc = _clean(dm.group(1)) if dm else ""
                        link = _clean(lm.group(1)) if lm else feed
                        pdate = None
                        if pm:
                            try:
                                pdate = datetime.strptime(
                                    pm.group(1).strip(), "%a, %d %b %Y %H:%M:%S %z")
                            except Exception:
                                pdate = None
                        if title:
                            parsed.append((
                                pdate or datetime.min.replace(tzinfo=timezone.utc),
                                title, desc, link))
                    parsed.sort(key=lambda x: x[0], reverse=True)
                    for _, title, desc, link in parsed[:max_eps]:
                        items.append(ContentItem(
                            title=title,
                            url=link,
                            summary=desc[:600],
                            source="xiaoyuzhou",
                            source_name=f"小宇宙·{pname[:18]}",
                            published=datetime.now(timezone.utc),
                        ))
                except Exception as ex:
                    logger.error(f"小宇宙 RSS {feed[:50]}: {ex}")

        logger.info(f"小宇宙: 获取 {len(items)} 条（来自 {len(feeds)} 个播客）")
        return items
