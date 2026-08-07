"""
中文搜索引擎 — 用 DuckDuckGo/Bing 每日搜索中文赚钱内容。

因为中文赚钱内容生态（知乎/即刻/小红书/B站等）几乎无法通过 RSS 直接获取，
改用搜索引擎每日定时搜索相关关键词，提取标题、链接、摘要。
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

# 每日搜索词 — 面向非程序员的赚钱机会
SEARCH_QUERIES = [
    "一人公司 赚钱 实操 案例",
    "副业 零成本 在家赚钱 教程",
    "小红书 赚钱 经验分享",
    "闲鱼 卖货 赚钱 教程",
    "自媒体 变现 案例 新手",
    "抖音 带货 普通人 赚多少",
    "知识付费 怎么做 从零开始",
    "AI 赚钱 不会编程 普通人",
    "跨境电商 一件代发 教程",
    "数字产品 卖模板 赚钱",
    "公众号 变现 月入 案例",
    "摆摊 小生意 赚钱 经验",
    "直播带货 新人 赚钱 攻略",
    "自由职业 接单 赚钱 平台",
    "不上班 靠什么赚钱 经验",
]

# 需要排除的域名（广告/低质站）
EXCLUDE_DOMAINS = [
    "zhihu.com",  # 知乎需要登录才能看全文，先排除
]


class ChineseSearchSource(BaseSource):
    """中文搜索引擎内容源。"""

    name = "chinese-search"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("中文搜索: 未启用，跳过")
            return []

        engine = cfg.get("engine", "bing")
        limit = cfg.get("limit", 5)
        delay = cfg.get("query_delay", 2)
        queries = cfg.get("queries", SEARCH_QUERIES)

        all_items: list[ContentItem] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        if engine == "bing":
            fetcher = self._fetch_bing
        elif engine == "duckduckgo":
            fetcher = self._fetch_duckduckgo
        else:
            logger.warning(f"中文搜索: 未知引擎 {engine}")
            return []

        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=headers
        ) as client:
            for i, query in enumerate(queries):
                # 每次搜索间延迟，避免被限流
                if i > 0 and delay > 0:
                    await asyncio.sleep(delay)
                try:
                    items = await fetcher(client, query, limit)
                    all_items.extend(items)
                except Exception as e:
                    logger.error(f"中文搜索 [{query}]: {e}")

        # 去重（按 URL）
        seen = set()
        unique = []
        for it in all_items:
            if it.url not in seen:
                seen.add(it.url)
                unique.append(it)

        logger.info(f"中文搜索: 获取到 {len(unique)} 条（去重后）")
        return unique[:cfg.get("max_total", 30)]

    async def _fetch_duckduckgo(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """DuckDuckGo HTML 搜索（202 也算成功，部分返回内容）。"""
        import asyncio as aio
        url = f"https://html.duckduckgo.com/html/?q={query}"
        for attempt in range(3):
            resp = await client.get(url)
            if resp.status_code in (200, 202):
                items = self._parse_ddg_html(resp.text, limit)
                if items:
                    return items
                if attempt < 2:
                    await aio.sleep(1)
                    continue
                return []
            logger.warning(f"DuckDuckGo [{query}]: HTTP {resp.status_code}")
            return []
        return []

    async def _fetch_bing(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """Bing 搜索（带重试）。"""
        import asyncio as aio
        url = f"https://www.bing.com/search?q={query}&setlang=zh-hans&count={limit}"
        for attempt in range(3):
            resp = await client.get(url)
            if resp.status_code == 200:
                items = self._parse_bing_html(resp.text, limit)
                if items:
                    return items
                if attempt < 2:
                    await aio.sleep(2)
                    continue
                return []
            logger.warning(f"Bing [{query}]: HTTP {resp.status_code}")
            return []
        return []

    def _parse_ddg_html(self, html: str, limit: int) -> list[ContentItem]:
        """解析 DuckDuckGo HTML 搜索结果。"""
        items = []
        # 每条结果的结构: class="result__body" 包含 title, snippet, url
        results = re.findall(
            r'class="result__body".*?</div>\s*</div>',
            html, re.DOTALL
        )
        for block in results[:limit]:
            # 提取标题
            title_m = re.search(
                r'class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL
            )
            if not title_m:
                continue
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()

            # 提取链接
            link_m = re.search(
                r'class="result__url"[^>]*>(.*?)</a>', block, re.DOTALL
            )
            link = ""
            if link_m:
                link_raw = re.sub(r'<[^>]+>', '', link_m.group(1)).strip()
                if link_raw.startswith("http"):
                    link = link_raw
                else:
                    link = f"https://{link_raw}"

            # 提取摘要
            snippet_m = re.search(
                r'class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL
            )
            snippet = ""
            if snippet_m:
                snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()[:300]

            # 跳过已排除域名
            skip = False
            for ex in EXCLUDE_DOMAINS:
                if ex in link:
                    skip = True
                    break
            if skip:
                continue

            items.append(ContentItem(
                title=title,
                url=link,
                summary=snippet,
                source="chinese-search",
                source_name="中文搜索",
                published=datetime.now(timezone.utc),
            ))

        return items

    def _parse_bing_html(self, html: str, limit: int) -> list[ContentItem]:
        """解析 Bing 搜索结果（备用，结构可能变化）。"""
        items = []
        # Bing 结果在 <li class="b_algo"> 中
        results = re.findall(
            r'<li class="b_algo".*?</li>',
            html, re.DOTALL
        )
        for block in results[:limit]:
            title_m = re.search(r'<h2>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
            if not title_m:
                continue
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()

            link_m = re.search(r'<h2>.*?<a[^>]*href="([^"]+)"', block)
            link = link_m.group(1) if link_m else ""

            snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            snippet = ""
            if snippet_m:
                snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()[:300]

            skip = False
            for ex in EXCLUDE_DOMAINS:
                if ex in link:
                    skip = True
                    break
            if skip:
                continue

            items.append(ContentItem(
                title=title,
                url=link,
                summary=snippet,
                source="chinese-search",
                source_name="中文搜索",
                published=datetime.now(timezone.utc),
            ))

        return items
