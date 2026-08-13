"""
中文搜索引擎 — 用 DuckDuckGo/Bing 每日搜索中文赚钱内容。

因为中文赚钱内容生态（知乎/即刻/小红书/B站等）几乎无法通过 RSS 直接获取，
改用搜索引擎每日定时搜索相关关键词，提取标题、链接、摘要。
"""

import asyncio
import logging
import re
from html import unescape
from datetime import datetime, timezone

import httpx
from urllib.parse import unquote, quote

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

        limit = cfg.get("limit", 5)
        delay = cfg.get("query_delay", 2)
        queries = cfg.get("queries", SEARCH_QUERIES)

        # 引擎级联：配置的引擎优先，命中反爬挑战页/0 结果时自动回退其他引擎。
        # 例：当前 DDG 常被限流返回 202 挑战页 → 自动切 Bing → Baidu，避免发现源长期 0 产出。
        primary = cfg.get("engine", "duckduckgo")
        # 回退顺序：DDG 限流时优先 Baidu（国内更相关、结果干净），Bing 作末位兜底
        fallback_pool = ("baidu", "bing") if primary != "baidu" else ("duckduckgo", "bing")
        order = [primary] + [e for e in fallback_pool if e != primary]
        fetchers = {
            "duckduckgo": self._fetch_duckduckgo,
            "bing": self._fetch_bing,
            "baidu": self._fetch_baidu,
        }

        all_items: list[ContentItem] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=headers
        ) as client:
            for engine in order:
                fetcher = fetchers.get(engine)
                if fetcher is None:
                    continue
                engine_items: list[ContentItem] = []
                for i, query in enumerate(queries):
                    if i > 0 and delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        engine_items.extend(await fetcher(client, query, limit))
                    except Exception as e:
                        logger.error(f"中文搜索[{engine}][{query}]: {e}")
                logger.info(f"中文搜索[{engine}]: 获取到 {len(engine_items)} 条")
                if engine_items:
                    all_items = engine_items
                    logger.info(f"中文搜索: 采用引擎 {engine}（{len(all_items)} 条）")
                    break
                logger.warning(f"中文搜索: 引擎 {engine} 无结果，回退下一个")

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
        """DuckDuckGo lite 搜索（html 端点已失效，切 lite 端点）。"""
        url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            # 202 = DDG 反爬挑战页，不是正常结果
            logger.warning(f"DuckDuckGo[{query}]: HTTP {resp.status_code}")
            return []
        if self._is_challenge(resp.text):
            logger.warning(f"DuckDuckGo[{query}]: 命中反爬挑战页，跳过")
            return []
        return self._parse_ddg_html(resp.text, limit)

    async def _fetch_bing(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """Bing 搜索（带重试）。"""
        import asyncio as aio
        url = f"https://www.bing.com/search?q={quote(query)}&setlang=zh-hans&count={limit}"
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

    @staticmethod
    def _ddg_real_url(href_raw: str) -> str:
        """DDG 结果链接形如 //duckduckgo.com/l/?uddg=<encoded>&rut=...，
        真实 URL 在 uddg 参数里（双重 URL 编码），解析出来。"""
        href = href_raw.replace("&amp;", "&")
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            return unquote(m.group(1))
        if href.startswith("//"):
            return "https:" + href
        return href

    def _parse_ddg_html(self, html: str, limit: int) -> list[ContentItem]:
        """解析 DuckDuckGo lite 搜索结果（class='result-link' 含标题/链接）。"""
        items = []
        # lite 端点 result-link 标签包含 id/href/class，属性顺序不固定
        tag_re = re.compile(r"<a\s[^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", re.DOTALL)
        href_re = re.compile(r"""href=['"]([^'"]+)['"]""")
        snippet_re = re.compile(r"<td\s+class='result-snippet'>\s*(.*?)\s*</td>", re.DOTALL)

        snippets = snippet_re.findall(html)

        for i, tag_html in enumerate(tag_re.finditer(html)):
            full = tag_html.group(0)  # 完整 <a ...>...</a>
            title = tag_html.group(1)  # 标签内文本
            title = re.sub(r"<[^>]+>", "", title).strip()
            title = unescape(title)
            hm = href_re.search(full)
            link = self._ddg_real_url(hm.group(1)) if hm else ""
            if not link or not title:
                continue
            if any(ex in link for ex in EXCLUDE_DOMAINS):
                continue
            snippet = ""
            if i < len(snippets):
                snippet = unescape(re.sub(r"<[^>]+>", "", snippets[i]).strip())[:300]
            items.append(ContentItem(
                title=title,
                url=link,
                summary=snippet,
                source="chinese-search",
                source_name="中文搜索",
                published=None,  # 搜索页只有相对时间(如"2小时前")，解析不出绝对时间；标 None 而非伪造"今天"
            ))
            if len(items) >= limit:
                break
        return items

    def _parse_bing_html(self, html: str, limit: int) -> list[ContentItem]:
        """解析 Bing 搜索结果。

        Bing 当前结构：<li class="b_algo"> 内含 <a class="tilk" ...> 标题锚点，
        链接是 bing.com/ck/a?...&u=a1<base64> 重定向，真实 URL 藏在 u 参数里（base64）。
        """
        items = []
        # 直接抓每个结果的标题锚点 <a class="tilk" ...>，标题在锚点文本或 aria-label
        anchors = re.findall(
            r'<a class="tilk"[^>]*?(?:aria-label="([^"]*)")?[^>]*?href="([^"]+)"[^>]*?>(.*?)</a>',
            html, re.DOTALL,
        )
        for aria_label, href, inner in anchors[:limit]:
            if aria_label:
                title = aria_label.strip()
            else:
                txt = re.sub(r"<[^>]+>", "", inner)
                txt = re.sub(r"https?://\S+", "", txt)  # 去掉内嵌网址面包屑
                title = unescape(txt.replace(" › ", " ").strip())
            if not title:
                continue
            link = self._bing_real_url(href)

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
                summary="",
                source="chinese-search",
                source_name="中文搜索",
                published=None,  # 搜索页只有相对时间(如"2小时前")，解析不出绝对时间；标 None 而非伪造"今天"
            ))
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _bing_real_url(href: str) -> str:
        """Bing 结果链接形如 bing.com/ck/a?...&u=a1<base64>，解出真实 URL。"""
        import base64 as _b64
        href = href.replace("&amp;", "&")  # HTML 实体化的 & 还原
        m = re.search(r"[?&]u=a1([^&]+)", href)
        if not m:
            return href
        s = m.group(1).replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        try:
            return _b64.b64decode(s).decode("utf-8", "ignore")
        except Exception:
            return href

    async def _fetch_baidu(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """Baidu 搜索（DDG 被限流时的回退引擎）。"""
        url = f"https://www.baidu.com/s?wd={quote(query)}&rn={limit}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"Baidu[{query}]: HTTP {resp.status_code}")
            return []
        if self._is_challenge(resp.text):
            logger.warning(f"Baidu[{query}]: 命中反爬挑战页，跳过")
            return []
        return self._parse_baidu_html(resp.text, limit)

    def _parse_baidu_html(self, html: str, limit: int) -> list[ContentItem]:
        """解析 Baidu 搜索结果（取 <h3><a href> 标题/链接）。"""
        items = []
        # 结果标题块：<h3 ...><a href="...">标题</a></h3>
        blocks = re.findall(r"<h3[^>]*>(.*?)</h3>", html, re.DOTALL)
        for blk in blocks[: limit * 2]:
            a = re.search(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', blk, re.DOTALL)
            if not a:
                continue
            link = a.group(1)
            title = unescape(re.sub(r"<[^>]+>", "", a.group(2)).strip())
            if not link or not title:
                continue
            if any(ex in link for ex in EXCLUDE_DOMAINS):
                continue
            items.append(ContentItem(
                title=title,
                url=link,
                summary="",
                source="chinese-search",
                source_name="中文搜索",
                published=None,  # 搜索页只有相对时间(如"2小时前")，解析不出绝对时间；标 None 而非伪造"今天"
            ))
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _is_challenge(html: str) -> bool:
        """检测搜索引擎反爬挑战页。

        只认强挑战信号，避免正常结果页里偶发的 robot/challenge 字样造成误杀
        （Bing 正常页就含 robot 字样，曾被误判为挑战页而跳过）。
        """
        low = (html or "").lower()
        return any(k in low for k in (
            "anomaly", "unusual traffic", "verify you are human",
            "请完成安全验证", "are you a robot", "robot check",
            "captcha", "security check", "人机验证",
        ))
