"""
公众号目标号轮询源 — 按「指定号名」搜索，而非泛关键词。

设计要点（2026-08-08 实测结论 + 本轮改进）：
- 公众号被微信锁死，无「既零部署又稳定可控」方案。用户放弃 WeWe-RSS 部署后，
  验证发现：DDG 在本环境易被限流（challenge 页）；Baidu 对 site:mp.weixin 结果做了
  h3/mu 双重混淆，需从 mu= 属性抓真实直链；搜狗微信(weixin.sogou.com)是专用引擎但
  偶发 wx_sh2 反爬。故本源采用「多引擎顺序尝试」：sogou → baidu → ddg，任一有结果即用。
- 机制：对每个目标号，用选中的引擎搜 `号名` → 跟进链接拿真实 mp.weixin.qq.com 文章页
  → 抓页提取 `var nickname` 验证作者是否真匹配目标号（号名 ≠ 作者显示名时以 weixin_id
  验证，如「临公子的后花园」作者显示「临公子」）。
- 「上次推送到这次之间有没有新发文」由 main.py 的 HistoryManager 持久化去重天然实现
  （看过的 URL 30 天内不再推），本源只负责把候选文章找出来。
- 增删号：直接改 storage/weixin_targets.json（或告诉我，我帮你加）。
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ROOT / "storage" / "weixin_targets.json"

HEAD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://weixin.sogou.com/",
}

# 作者昵称提取（mp.weixin 文章页）
NICK_RE = [
    re.compile(r"var\s+nickname\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"[\"']nickname[\"']\s*:\s*[\"']([^\"']+)[\"']"),
    re.compile(r"property=[\"']og:article:author[\"']\s+content=[\"']([^\"']+)[\"']", re.I),
    re.compile(r"class=[\"']profile_nickname[\"'][^>]*>([^<]+)<"),
    re.compile(r"id=[\"']js_name[\"'][^>]*>([^<]+)<"),
]

# 文章标题提取（mp.weixin 文章页）
TITLE_RE = [
    re.compile(r"var\s+msg_title\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"<title>([^<]+)</title>"),
]

# 文章摘要提取（mp.weixin 文章页）：description / og:description / msg_desc
SUMMARY_RE = [
    re.compile(r"var\s+msg_desc\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"<meta\s+name=[\"']description[\"']\s+content=[\"']([^\"']+)[\"']", re.I),
    re.compile(r"property=[\"']og:description[\"']\s+content=[\"']([^\"']+)[\"']", re.I),
]


# ============================================================
# 纯函数：解析 / 提取（便于离线测试）
# ============================================================

def extract_nickname(html: str) -> str:
    """从 mp.weixin 文章页提取作者昵称。匹配不到返回空串。"""
    if not html:
        return ""
    for rx in NICK_RE:
        m = rx.search(html)
        if m:
            return m.group(1).strip()
    return ""


def extract_title(html: str) -> str:
    """从 mp.weixin 文章页提取文章标题。匹配不到返回空串。"""
    if not html:
        return ""
    for rx in TITLE_RE:
        m = rx.search(html)
        if m:
            t = m.group(1).strip()
            # <title> 通常含 " - 公众号名" 后缀，剥离
            t = re.split(r"\s*[-|]\s*微信", t)[0]
            t = re.split(r"\s*[-|]\s*$", t)[0]
            return t.strip()
    return ""


def extract_summary(html: str) -> str:
    """从 mp.weixin 文章页提取文章摘要（description / og:description / msg_desc）。"""
    if not html:
        return ""
    for rx in SUMMARY_RE:
        m = rx.search(html)
        if m:
            return m.group(1).strip()
    return ""


def extract_publish_time(html: str) -> Optional[datetime]:
    """从 mp.weixin 文章页提取发布时间（var publish_time，Unix 秒）。匹配不到返回 None。

    长周期深度文章发布久≠价值低，保留真实时间让卡片显示真实日期、评分也能感知年龄。
    """
    if not html:
        return None
    m = re.search(r"var\s+publish_time\s*=\s*[\"']?(\d{10})[\"']?", html)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        except Exception:
            return None
    return None


def _norm(s: str) -> str:
    """归一化：去空白（含全角空格）。"""
    return re.sub(r"\s+", "", s or "")


def nickname_matches(nick: str, weixin_id: str) -> bool:
    """作者昵称是否匹配目标号。归一化精确匹配，失败再容错子串匹配。"""
    if not nick or not weixin_id:
        return False
    n, w = _norm(nick), _norm(weixin_id)
    if n == w:
        return True
    # 容错：显示名可能是目标号的子串（如「临公子」vs「临公子的后花园」已用 weixin_id=临公子）
    if len(w) >= 2 and w in n:
        return True
    if len(n) >= 2 and n in w:
        return True
    return False


def parse_sogou_results(html: str, limit: int = 8) -> list[tuple[str, str]]:
    """解析搜狗微信结果页，提取文章跳转链接。

    搜狗微信文章链接形如 `https://weixin.sogou.com/link?url=...`，
    跟进 302 重定向即落到真实 mp.weixin.qq.com/s/... 文章页。
    """
    if not html:
        return []
    links = re.findall(
        r'href="(https?://weixin\.sogou\.com/link\?url=[^"]+)"', html
    )
    seen, out = set(), []
    for raw in links:
        if raw in seen:
            continue
        seen.add(raw)
        out.append(("", raw))
        if len(out) >= limit * 2:
            break
    return out


def parse_baidu_results(html: str, limit: int = 8) -> list[tuple[str, str]]:
    """解析 Baidu 结果页，提取真实公众号文章直链。

    注意：Baidu 结果页的 <h3> 标题链接是被混淆的跳转串（多条标题共用同一
    `baidu.com/link?url=...`，跟进后只落同一篇），真实文章直链藏在每条结果的
    `mu="https://mp.weixin.qq.com/s?__biz=..."` 属性里。故直接抓 mu 属性里的直链。
    """
    if not html:
        return []
    mus = re.findall(r'mu=["\'](https?://mp\.weixin\.qq\.com/[^"\']+)["\']', html)
    seen, out = set(), []
    for raw in mus:
        link = raw.replace("&amp;", "&")
        if link in seen:
            continue
        seen.add(link)
        out.append(("", link))
        if len(out) >= limit * 2:
            break
    return out


def parse_ddg_results(html: str, limit: int = 8) -> list[tuple[str, str]]:
    """解析 DDG html（site:mp.weixin.qq.com）结果，提取真实微信文章链接。"""
    if not html:
        return []
    titles = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL
    )
    from urllib.parse import unquote

    out = []
    for href_raw, th in titles:
        href = href_raw.replace("&amp;", "&")
        m = re.search(r"[?&]uddg=([^&]+)", href)
        link = unquote(m.group(1)) if m else href
        if "mp.weixin.qq.com/s/" not in link:
            continue
        title = re.sub(r"<[^>]+>", "", th).strip()
        out.append((title, link))
        if len(out) >= limit:
            break
    return out


def load_targets(path: Optional[Path] = None) -> list[dict]:
    """读取目标公众号列表 JSON。字段：name / weixin_id(验证用显示名) / search / note。"""
    p = path or DEFAULT_TARGETS
    if not p.exists():
        logger.warning(f"目标公众号列表不存在: {p}")
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("目标公众号列表格式应为 JSON 数组")
            return []
        return data
    except Exception as e:
        logger.error(f"读取目标公众号列表失败: {e}")
        return []


# ============================================================
# 源实现
# ============================================================

class WeixinTargetSource(BaseSource):
    """公众号目标号轮询源：多引擎按号名搜索 + 作者昵称验证。"""

    name = "weixin-targets"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("公众号目标号轮询: 未启用，跳过")
            return []

        targets = load_targets()
        if not targets:
            logger.info("公众号目标号轮询: 列表为空，跳过")
            return []

        engines = cfg.get("engines", ["sogou", "baidu", "ddg"])
        per_account = cfg.get("per_account", 4)
        delay = cfg.get("delay", 2)
        max_total = cfg.get("max_total", 20)
        timeout = cfg.get("timeout", 15)

        all_items: list[ContentItem] = []
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=HEAD
        ) as client:
            for t in targets:
                name = t.get("name", "")
                weixin_id = t.get("weixin_id") or name
                search = t.get("search") or weixin_id
                if not search:
                    continue
                try:
                    items = await self._search_account(
                        client, engines, search, weixin_id, per_account, timeout
                    )
                    for it in items:
                        it.source_name = f"公众号·{name}"
                    all_items.extend(items)
                except Exception as e:
                    logger.error(f"公众号目标号轮询 [{name}]: {e}")
                if delay > 0:
                    await asyncio.sleep(delay)

        # 去重（按真实文章 URL）
        seen, unique = set(), []
        for it in all_items:
            key = it.url or it.title
            if key not in seen:
                seen.add(key)
                unique.append(it)

        logger.info(f"公众号目标号轮询: 获取 {len(unique)} 条（去重后）")
        return unique[:max_total]

    async def _search_account(
        self,
        client: httpx.AsyncClient,
        engines: list[str],
        search: str,
        weixin_id: str,
        per_account: int,
        timeout: int,
    ) -> list[ContentItem]:
        """按引擎顺序搜单个目标号，任一引擎有候选即停止，返回作者验证通过的条目。"""
        items: list[ContentItem] = []
        candidates: list[tuple[str, str]] = []
        for engine in engines:
            try:
                if engine == "sogou":
                    candidates = await self._sogou_search(client, search, per_account, timeout)
                elif engine == "baidu":
                    candidates = await self._baidu_search(client, search, per_account, timeout)
                else:
                    candidates = await self._ddg_search(client, search, per_account, timeout)
            except Exception as e:
                logger.debug(f"  引擎 {engine} 搜 [{search}] 异常: {e}")
                candidates = []
            if candidates:
                logger.info(f"  [{search}] 引擎 {engine} 命中 {len(candidates)} 候选")
                break
        else:
            logger.info(f"  [{search}] 所有引擎 0 候选（可能限流/反爬）")
            return items

        for title, link in candidates:
            try:
                nick, final_url, art_title, summary, pub_dt = await self._verify_author(client, link, timeout)
            except Exception as e:
                logger.debug(f"  验证作者失败 {link}: {e}")
                continue
            if nickname_matches(nick, weixin_id):
                items.append(ContentItem(
                    title=art_title or title or (nick or weixin_id),
                    url=final_url or link,
                    summary=summary,
                    source="weixin_targets",
                    source_name="公众号",
                    published=pub_dt or datetime.now(timezone.utc),
                ))
                logger.info(f"  ✅ 命中目标号 [{weixin_id}]: {(art_title or nick)[:30]}")
            else:
                logger.debug(f"  ❌ 作者不匹配({nick!r}≠{weixin_id!r})")
        return items

    async def _sogou_search(
        self, client: httpx.AsyncClient, search: str, limit: int, timeout: int
    ) -> list[tuple[str, str]]:
        from urllib.parse import quote

        # type=2 按关键词搜文章（含目标号发文）；type=1 是按号找号，这里用 2 直接拿文章
        q = f"https://weixin.sogou.com/weixin?type=2&query={quote(search)}&ie=utf8"
        try:
            r = await client.get(q, timeout=timeout)
        except Exception as e:
            logger.error(f"搜狗微信搜索失败 [{search}]: {e}")
            return []
        if r.status_code != 200 or not r.text:
            logger.warning(f"搜狗微信 [{search}]: HTTP {r.status_code}")
            return []
        return parse_sogou_results(r.text, limit * 2)

    async def _baidu_search(
        self, client: httpx.AsyncClient, search: str, limit: int, timeout: int
    ) -> list[tuple[str, str]]:
        from urllib.parse import quote

        q = f'site:mp.weixin.qq.com "{search}"'
        try:
            r = await client.get(
                "https://www.baidu.com/s?wd=" + quote(q), timeout=timeout
            )
        except Exception as e:
            logger.error(f"Baidu 搜索失败 [{search}]: {e}")
            return []
        if r.status_code != 200:
            logger.error(f"Baidu 搜索 [{search}]: HTTP {r.status_code}")
            return []
        return parse_baidu_results(r.text, limit * 2)

    async def _ddg_search(
        self, client: httpx.AsyncClient, search: str, limit: int, timeout: int
    ) -> list[tuple[str, str]]:
        from urllib.parse import quote

        q = f'site:mp.weixin.qq.com "{search}"'
        try:
            r = await client.post(
                "https://html.duckduckgo.com/html/", data={"q": q}, timeout=timeout
            )
        except Exception as e:
            logger.error(f"DDG 搜索失败 [{search}]: {e}")
            return []
        if r.status_code not in (200, 202):
            return []
        return parse_ddg_results(r.text, limit)

    async def _verify_author(
        self, client: httpx.AsyncClient, link: str, timeout: int
    ) -> tuple[str, str, str, str, Optional[datetime]]:
        """抓文章页（自动跟随 Redirect 到 mp.weixin），返回 (作者昵称, 最终文章URL, 文章标题, 摘要, 发布时间)。

        最终 URL 既用于内容去重，也用于推送时打开真实文章（而非搜狗/百度的跳转链）。
        摘要直接取自文章页 description，使 AI 评估不依赖经常失败的 enricher 全文抓取。
        发布时间取自 var publish_time（解析不到为 None），用于卡片显示真实日期、评分感知年龄。
        """
        try:
            rp = await client.get(link, timeout=timeout)
        except Exception:
            return "", link, "", "", None
        final = str(rp.url)
        # 兼容两种真实文章 URL：/s/xxxx（搜狗跟进后）与 /s?__biz=...（Baidu mu= 直链）
        if "mp.weixin.qq.com/s" not in final:
            return "", final, "", "", None  # 不是公众号文章（如搜狗产品页 / 荐号软文）
        return (
            extract_nickname(rp.text),
            final,
            extract_title(rp.text),
            extract_summary(rp.text),
            extract_publish_time(rp.text),
        )
