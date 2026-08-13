"""
公众号搜索源 — 用搜狗微信搜索发现公众号「一人公司/副业/赚钱」文章，
再用 DuckDuckGo 反查真实 mp.weixin 链接并抓全文。

设计要点：
- 搜狗微信搜索（weixin.sogou.com）对「公众号」覆盖最全、最新，用来发现标题/摘要。
  但它对密集请求会出反爬页（已加重试），且文章真实链接藏在 /link?url= 的 JS 跳转里，解析不稳。
- 真实文章链接改用 DuckDuckGo 反查：site:mp.weixin.qq.com "<标题>" → 直接拿到 mp.weixin 真实 URL，
  DDG 在本环境稳定可用，且 trafilatura 对该 UA 能稳定抓到正文（实测 900+ 字）。
- 两步解析：先试搜狗 /link?url= 跳转（快），失败再用 DDG 反查（稳）。
- 全部失败则保留搜狗中转链接（浏览器里仍会跳转到真实文章），并以摘要作为内容兜底。
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin

import httpx

from .base import BaseSource, ContentItem
from .weixin_targets import extract_publish_time

logger = logging.getLogger(__name__)

# 每日搜索词 — 面向「不会写代码的人」的一人公司/副业/变现实操
SEARCH_QUERIES = [
    "一人公司 赚钱 案例 复盘",
    "副业 月入过万 实操 普通人",
    "公众号 变现 月入 真实 案例",
    "小红书 变现 案例 一个人",
    "知识付费 赚钱 真实 复盘",
    "自由职业 接单 赚钱 经验",
    "AI 副业 普通人 赚钱 实操",
    "不上班 赚钱 案例 一个人",
    "小本创业 赚钱 方法 经验",
    "个人IP 变现 案例 怎么做",
    "闲鱼 卖货 赚钱 实操 教程",
    "付费社群 变现 案例 单人",
]

# 浏览器 UA（搜狗/微信对爬虫 UA 会限流或出验证码）
HEAD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weixin.sogou.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _clean(t: str) -> str:
    """去 HTML 标签 / 注释 / 实体，得到干净文本。"""
    if not t:
        return ""
    t = re.sub(r"<!--.*?-->", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in [
        ("&amp;", "&"), ("&nbsp;", " "), ("&ldquo;", "“"), ("&rdquo;", "”"),
        ("&hellip;", "…"), ("&middot;", "·"), ("&quot;", '"'), ("&#39;", "'"),
    ]:
        t = t.replace(a, b)
    return t.strip()


class WeixinSearchSource(BaseSource):
    """公众号搜索内容源（搜狗微信发现 + DDG 反查真实链接）。"""

    name = "weixin-search"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("公众号搜索: 未启用，跳过")
            return []

        limit = cfg.get("limit", 6)
        delay = cfg.get("query_delay", 3)
        queries = cfg.get("queries", SEARCH_QUERIES)
        max_total = cfg.get("max_total", 20)

        all_items: list[ContentItem] = []
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=HEAD
        ) as client:
            for i, query in enumerate(queries):
                if i > 0 and delay > 0:
                    await asyncio.sleep(delay)
                try:
                    items = await self._search(client, query, limit)
                    all_items.extend(items)
                except Exception as e:
                    logger.error(f"公众号搜索 [{query}]: {e}")

        # 去重（按真实文章 URL；解析失败的用 sogou 中转链接兜底）
        seen, unique = set(), []
        for it in all_items:
            key = it.url or it.title
            if key not in seen:
                seen.add(key)
                unique.append(it)

        # 补真实发布时间（公众号深度文，发布久≠过时；与小宇宙/目标号对齐，不伪造"今天"）。
        # 仅在去重后的最终候选上 best-effort 抓一次，成本受 max_total 上限约束。
        if unique:
            await self._enrich_publish_times(client, unique)

        logger.info(f"公众号搜索: 获取 {len(unique)} 条（去重后）")
        return unique[:max_total]

    async def _enrich_publish_times(
        self, client: httpx.AsyncClient, items: list[ContentItem]
    ) -> None:
        """对每条公众号文章 best-effort 抓取真实发布时间，覆盖源构造时的占位时间。

        复用 weixin_targets.extract_publish_time（从 var publish_time 解析）。
        失败（限流/非文章页/超时）保持原占位时间，不阻断整条采集。
        """
        for it in items:
            if it.source != "weixin" or "mp.weixin.qq.com" not in (it.url or ""):
                continue
            try:
                rp = await client.get(it.url, timeout=10)
            except Exception:
                continue
            if "mp.weixin.qq.com/s" not in str(rp.url):
                continue
            pt = extract_publish_time(rp.text)
            if pt:
                it.published = pt

    async def _search(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """发现公众号文章：DDG 的 site:mp.weixin.qq.com 优先（稳定，避开搜狗反爬），
        失败再回退搜狗账号搜索（优雅降级）。"""
        try:
            ddg_items = await self._search_ddg(client, query, limit)
            if ddg_items:
                return ddg_items
        except Exception as e:
            logger.error(f"DDG 公众号搜索失败 [{query}]: {e}")
        # 回退：搜狗账号搜索（可能出反爬页，优雅降级）
        return await self._search_sogou(client, query, limit)

    async def _search_ddg(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        """用 DuckDuckGo 的 site:mp.weixin.qq.com 发现公众号文章（稳定路径）。

        与 chinese_search 一致的解析：result__a=标题(uddg 编码真实链接)，
        result__snippet=摘要。只保留 mp.weixin.qq.com/s/ 的真实文章。
        """
        q = f"site:mp.weixin.qq.com {query}"
        url = "https://html.duckduckgo.com/html/?q=" + quote(q)
        try:
            r = await client.get(url, timeout=15)
        except Exception as e:
            logger.error(f"DDG 公众号搜索 [{query}]: {e}")
            return []
        if r.status_code not in (200, 202):
            return []
        items = self._parse_ddg_html(r.text, limit)
        logger.info(f"DDG 公众号搜索 [{query}]: {len(items)} 条")
        return items[:limit]

    @staticmethod
    def _parse_ddg_html(html: str, limit: int) -> list[ContentItem]:
        """解析 DDG html（site:mp.weixin.qq.com）结果，提取真实微信文章。

        纯函数，便于离线测试；只保留 mp.weixin.qq.com/s/ 的真实文章。
        """
        title_re = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
        )
        snippet_re = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
        )
        titles = title_re.findall(html)
        snippets = snippet_re.findall(html)
        items: list[ContentItem] = []
        for i, (href_raw, th) in enumerate(titles):
            href = href_raw.replace("&amp;", "&")
            m = re.search(r"[?&]uddg=([^&]+)", href)
            link = unquote(m.group(1)) if m else href
            if "mp.weixin.qq.com/s/" not in link:
                continue
            title = _clean(th)
            snip = _clean(snippets[i]) if i < len(snippets) else ""
            items.append(ContentItem(
                title=title,
                url=link,
                summary=snip,
                source="weixin",
                source_name="公众号·微信",
                published=datetime.now(timezone.utc),
            ))
            if len(items) >= limit:
                break
        return items[:limit]

    async def _search_sogou(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[ContentItem]:
        url = "https://weixin.sogou.com/weixin?type=2&query=" + quote(query)
        # 搜狗对密集请求会出反爬页，重试
        blocks = None
        for attempt in range(3):
            try:
                r = await client.get(url)
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                raise
            if r.status_code != 200:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return []
            found = re.findall(
                r'<div class="txt-box">.*?</div>\s*</div>', r.text, re.DOTALL
            )
            if found:
                blocks = found
                break
            if attempt < 2:
                await asyncio.sleep(3)
                continue
            return []

        items = []
        for b in blocks[: limit * 2]:
            hm = re.search(
                r'<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.DOTALL
            )
            if not hm:
                continue
            href, raw_title = hm.group(1), hm.group(2)
            title = _clean(raw_title)
            snip = re.search(r'class="txt-info"[^>]*>(.*?)</p>', b, re.DOTALL)
            snip = _clean(snip.group(1)) if snip else ""
            acct = re.search(r'class="s-p">(.*?)</p>', b, re.DOTALL)
            acct = _clean(acct.group(1)) if acct else "微信公众号"

            sogou_link = urljoin("https://weixin.sogou.com", href)
            # 解析真实链接：先试搜狗跳转，失败用 DDG 反查（用发现关键词，概念匹配更稳）
            real_url = await self._resolve_sogou(client, sogou_link)
            if not real_url:
                real_url = await self._resolve_ddg(client, query)
            final_url = real_url or sogou_link

            items.append(ContentItem(
                title=title,
                url=final_url,
                summary=snip,
                source="weixin",
                source_name=f"公众号·{acct[:20]}",
                published=datetime.now(timezone.utc),
            ))
        return items[:limit]

    async def _resolve_sogou(
        self, client: httpx.AsyncClient, sogou_link: str, depth: int = 0
    ) -> str | None:
        """跟搜狗 /link?url= 跳转，拿到真实 mp.weixin 文章链接（最佳努力）。"""
        if depth > 2:
            return None
        try:
            r = await client.get(sogou_link, follow_redirects=False, timeout=12)
        except Exception:
            return None

        # HTTP 跳转
        loc = r.headers.get("location")
        if loc:
            nxt = loc if loc.startswith("http") else urljoin(sogou_link, loc)
            if "mp.weixin.qq.com" in nxt:
                return nxt
            if "weixin.sogou.com" in nxt:
                return await self._resolve_sogou(client, nxt, depth + 1)
            return nxt

        # JS / meta 跳转
        body = r.text or ""
        # ▸ 拼接式跳转（搜狗 /link?url= 最常见形态）：
        #   var url=''; url+='https://mp.'; url+='weixin.qq.c'; url+='om/s?src=11&...'; window.location.replace(url)
        #   URL 被拆成多个 '片段' 用 url+='...' 拼起来，必须拼接才能拿到真实 mp.weixin 链接。
        parts = re.findall(r"url\s*\+=\s*['\"]([^'\"]*)['\"]", body)
        if parts:
            joined = "".join(parts)
            if "mp.weixin.qq.com" in joined:
                return joined
        for pat in [
            r'window\.location\.href\s*=\s*["\']([^"\']+)',
            r'window\.location\.replace\(["\']([^"\']+)',
            r'document\.location\s*=\s*["\']([^"\']+)',
            r'var\s+url\s*=\s*["\']([^"\']+)',
            r'content\s*=\s*["\']\d+;url=([^"\']+)',
        ]:
            m = re.search(pat, body)
            if m:
                u = m.group(1)
                if u.startswith("//"):
                    u = "https:" + u
                elif u.startswith("/"):
                    u = urljoin(sogou_link, u)
                if "mp.weixin.qq.com" in u:
                    return u
                if "weixin.sogou.com" in u:
                    return await self._resolve_sogou(client, u, depth + 1)
                return u
        return None

    async def _resolve_ddg(
        self, client: httpx.AsyncClient, query: str
    ) -> str | None:
        """用 DuckDuckGo 反查真实 mp.weixin 链接（搜狗跳转解析失败时的稳路）。

        用发现时的关键词做概念式搜索（site:mp.weixin.qq.com <关键词>），
        比精确标题匹配更稳，能拿到同主题的真实公众号文章链接。
        """
        q = f"site:mp.weixin.qq.com {query}"
        url = "https://html.duckduckgo.com/html/?q=" + quote(q)
        try:
            r = await client.get(url, timeout=15)
        except Exception:
            return None
        if r.status_code not in (200, 202):
            return None
        for enc in re.findall(r"[?&]uddg=([^&]+)", r.text):
            dec = unquote(enc)
            if "mp.weixin.qq.com/s/" in dec:
                return dec
        return None
