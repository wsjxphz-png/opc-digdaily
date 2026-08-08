"""
B站（哔哩哔哩）源 — 免登录抓取「一人公司 / 副业 / 自媒体变现」相关视频。

设计要点：
- 用 B站公开搜索 API `api.bilibili.com/x/web-interface/search/all/v2`（已验证无需 WBI
  签名即可从数据中心 IP 读取，返回 code=0）。按关键词（一人公司/副业/个人IP…）搜视频。
- 结果里 video 分组的每条含：title(带 <em class="keyword"> 高亮标签需剥离)、author、
  bvid、arcurl、description、tag、pubdate(Unix 时间戳)、play、pic。
- 单条视频即一个内容条目，summary = 描述 + 标签；source_name 带「B站·作者」便于溯源。
- 因为是「按 OPC 主题订阅」搜得，天然对题，主流程预筛阶段豁免二次关键词过滤
  （main.py Phase2 已处理）。
- 可选 fetch_transcript：抓 AI 字幕补全文（需额外 2 请求/视频，默认关闭以保稳定；
  后续要更强信号时再开）。
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/all/v2"
HEAD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://search.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

DEFAULT_QUERIES = [
    "一人公司", "副业 赚钱", "个人IP 变现", "自媒体 怎么赚钱",
    "不上班 赚钱", "小本创业", "知识付费 怎么做", "小红书 变现",
    "自由职业 接单", "AI 副业 普通人",
]

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """剥离 HTML 标签（B站 title 含 <em class="keyword"> 高亮）。"""
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def parse_bilibili_results(json_obj: dict, limit: int = 10) -> list[dict]:
    """从 B站搜索 JSON 提取 video 分组条目（纯函数，便于离线测试）。"""
    out = []
    if not isinstance(json_obj, dict):
        return out
    groups = json_obj.get("data", {}).get("result", []) or []
    for grp in groups:
        if grp.get("result_type") != "video":
            continue
        for v in grp.get("data", []) or []:
            bvid = v.get("bvid")
            if not bvid:
                continue
            title = _clean(v.get("title", ""))
            if not title:
                continue
            out.append({
                "bvid": bvid,
                "title": title,
                "author": v.get("author", "") or v.get("uname", ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "description": (v.get("description") or "").strip(),
                "tag": v.get("tag", "") or "",
                "pic": v.get("pic", "") or "",
                "play": v.get("play", 0) or 0,
                "pubdate": v.get("pubdate", 0) or 0,
            })
            if len(out) >= limit:
                return out
    return out


class BilibiliSource(BaseSource):
    """B站视频源：关键词搜视频 → 标准化条目。"""

    name = "bilibili"

    async def fetch(self, cfg: dict, keywords: dict, client=None) -> list[ContentItem]:
        if not cfg.get("enabled", False):
            logger.info("B站: 未启用，跳过")
            return []

        if client is not None:
            return await self._run(client, cfg)

        async with httpx.AsyncClient(
            timeout=cfg.get("timeout", 20), follow_redirects=True, headers=HEAD
        ) as client:
            return await self._run(client, cfg)

    async def _run(self, client: httpx.AsyncClient, cfg: dict) -> list[ContentItem]:
        queries = cfg.get("queries", DEFAULT_QUERIES)
        max_per_query = cfg.get("max_per_query", 5)
        max_total = cfg.get("max_total", 20)
        delay = cfg.get("query_delay", 2)

        items: list[ContentItem] = []
        for q in queries:
            if len(items) >= max_total:
                break
            try:
                raw = await client.get(
                    SEARCH_API, params={"keyword": q, "page": 1}
                )
                if raw.status_code != 200:
                    logger.warning(f"B站搜索 [{q}]: HTTP {raw.status_code}")
                    continue
                data = raw.json()
                if data.get("code") != 0:
                    logger.warning(f"B站搜索 [{q}]: code={data.get('code')} {data.get('message')}")
                    continue
                vids = parse_bilibili_results(data, max_per_query)
                for v in vids:
                    if len(items) >= max_total:
                        break
                    summary = v["description"]
                    if v["tag"]:
                        summary = (summary + "\n标签：" + v["tag"]).strip()
                    pub = None
                    if v["pubdate"]:
                        pub = datetime.fromtimestamp(v["pubdate"], tz=timezone.utc)
                    items.append(ContentItem(
                        title=v["title"],
                        url=v["url"],
                        summary=summary[:800],
                        source="bilibili",
                        source_name=f"B站·{v['author'][:16]}",
                        published=pub or datetime.now(timezone.utc),
                        thumbnail=v["pic"],
                    ))
                logger.info(f"B站 [{q}]: 获取 {len(vids)} 条")
            except Exception as e:
                logger.error(f"B站搜索 [{q}]: {e}")
            if delay > 0:
                await asyncio.sleep(delay)

        logger.info(f"B站: 获取 {len(items)} 条")
        return items
