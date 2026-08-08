"""
公众号白名单源 — 定向轮询「写一人公司/OPC 最牛」或本身就是牛逼 OPC 的公众号名单，
每天拉它们的新文章做筛选。

为什么需要：关键词搜索（weixin_search）偶发被搜狗反爬干到 0 条；而「先定一批高质量账号、
每天定点看它们发了什么」更稳、更对题——这正是用户要的「名单制」采集。

实现：
- 默认路径：搜狗微信「账号搜索」(type=2&query=账号名) 拿到该账号最近文章（复用 WeixinSearchSource 的
  跳转解析，已验证能解出 mp.weixin 真实链接并抓全文）。
- 可选稳定路径（用户部署 WeWe-RSS 后）：配置 rss_base_url，直接拉 RSS（基于微信读书，稳定不被限流）。
"""

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import BaseSource, ContentItem
from .weixin_search import WeixinSearchSource

logger = logging.getLogger(__name__)

# 项目根目录（用于定位动态源注册表 storage/scouted_sources.json）
_ROOT = Path(__file__).parent.parent
_DYNAMIC_FILE = _ROOT / "storage" / "scouted_sources.json"

HEAD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weixin.sogou.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class WeixinWhitelistSource(BaseSource):
    name = "weixin-whitelist"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("公众号白名单: 未启用，跳过")
            return []
        accounts = cfg.get("accounts", [])
        if not accounts:
            logger.info("公众号白名单: 未配置账号，跳过")
            return []
        # 合并动态源注册表（侦察兵每日新增的账号），跳过 config 已有权限，避免重复轮询
        accounts = self._merge_dynamic(accounts)

        limit = cfg.get("limit_per_account", 3)
        delay = cfg.get("query_delay", 3)
        max_total = cfg.get("max_total", 30)

        # 可选稳定路径：WeWe-RSS（基于微信读书，稳定不被限流）
        base = (cfg.get("rss_base_url") or "").strip().rstrip("/")
        if base:
            items = await self._fetch_rss(base, accounts, limit)
            if items:
                logger.info(f"公众号白名单: WeWe-RSS 拉取 {len(items)} 条")
                return self._dedupe(items)[:max_total]
            logger.warning("WeWe-RSS 拉取为空/失败，回退搜狗账号搜索")

        # 默认路径：搜狗账号搜索（复用 WeixinSearchSource 的跳转解析）
        searcher = WeixinSearchSource()
        items: list[ContentItem] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEAD) as client:
            for i, acct in enumerate(accounts):
                if i > 0 and delay > 0:
                    await asyncio.sleep(delay)
                try:
                    found = await searcher._search(client, acct, limit)
                    for it in found:
                        it.source = "weixin_whitelist"
                        it.source_name = f"公众号·{acct}"
                    items.extend(found)
                    logger.info(f"公众号白名单 [{acct}]: 获取 {len(found)} 条")
                except Exception as e:
                    logger.error(f"公众号白名单 [{acct}]: {e}")

        return self._dedupe(items)[:max_total]

    async def _fetch_rss(
        self, base: str, accounts: list[str], limit: int
    ) -> list[ContentItem]:
        """WeWe-RSS 稳定路径：拉全部订阅的 RSS，再按白名单账号名过滤。"""
        url = base + "/feeds/all.rss"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEAD) as client:
                r = await client.get(url)
            if r.status_code != 200:
                return []
            return self._parse_rss(r.text, accounts, limit)
        except Exception as e:
            logger.error(f"WeWe-RSS 拉取失败: {e}")
            return []

    @staticmethod
    def _parse_rss(xml_text: str, accounts: list[str], limit: int) -> list[ContentItem]:
        """解析 WeWe-RSS 的 all.rss（纯函数，便于离线测试）。

        关键：保留每条文章的「公众号名」到 source_name（如 公众号·阿猫读书），
        这样内容源侦察兵才能从已采集内容里挖到账号名、自动扩充白名单。
        """
        blocks = re.findall(r"<item>.*?</item>", xml_text, re.DOTALL)
        acct_set = {a.strip().lower() for a in accounts}
        items: list[ContentItem] = []
        for b in blocks:
            title = re.sub(r"<[^>]+>", "", _tag(b, "title") or "").strip()
            link = (_tag(b, "link") or "").strip()
            desc = re.sub(r"<[^>]+>", "", _tag(b, "description") or "").strip()
            # 公众号名：优先 author / dc:creator，其次标题里的「账号名：文章标题」前缀
            acct = (_tag(b, "author") or _tag(b, "dc:creator") or "").strip()
            if not acct and "：" in title:
                acct = title.split("：", 1)[0].strip()
            acct = acct or "微信公众号"
            # 按白名单账号名粗匹配（大小写不敏感、子串）
            if acct_set and not any(a in acct.lower() for a in acct_set):
                # 也允许标题/描述里出现白名单账号名
                if not any(a in f"{title} {desc}".lower() for a in acct_set):
                    continue
            items.append(ContentItem(
                title=title,
                url=link,
                summary=desc[:200],
                source="weixin_whitelist",
                source_name=f"公众号·{acct[:20]}",
                published=datetime.now(timezone.utc),
            ))
            if len(items) >= limit * max(1, len(accounts)):
                break
        return items


    @staticmethod
    def _dedupe(items: list[ContentItem]) -> list[ContentItem]:
        seen, unique = set(), []
        for it in items:
            key = it.url or it.title
            if key not in seen:
                seen.add(key)
                unique.append(it)
        return unique

    @staticmethod
    def _merge_dynamic(accounts: list[str]) -> list[str]:
        """并入动态源注册表（侦察兵每日新增的账号），跳过 config 已有权限。

        这样即使侦察兵当天没跑/被关，历史上通过评估的账号仍会被持续轮询。
        与 main 在收集前注入的动态账号互补：main 注入的已在 accounts 里，这里会跳过，不重复。
        """
        if not _DYNAMIC_FILE.exists():
            return accounts
        try:
            reg = json.loads(_DYNAMIC_FILE.read_text(encoding="utf-8"))
        except Exception:
            return accounts
        approved = [
            e.get("name")
            for e in reg.get("entries", [])
            if e.get("verdict") == "add" and e.get("platform") == "weixin"
        ]
        base = list(accounts)
        added = 0
        for a in approved:
            if a and a not in base:
                base.append(a)
                added += 1
        if added:
            logger.info(f"公众号白名单: 并入动态源 {added} 个（侦察兵累计 {len(approved)} 个）")
        return base


def _tag(block: str, name: str) -> str | None:
    """从一段 XML 里取某个标签的文本（兼容 CDATA / 命名空间前缀）。"""
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.DOTALL)
    if not m:
        return None
    txt = m.group(1)
    txt = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", txt, flags=re.DOTALL)
    return txt.strip()
