"""
全文提取模块 — 抓取文章正文和 Reddit 评论，让 AI 基于完整内容判断质量。

RSS 文章：下载 HTML → trafilatura 提取正文（≤5000 字）
Reddit 帖子：请求 JSON API → 提取前 3 条高赞评论
"""

import asyncio
import logging
import re
import trafilatura
import httpx

from .base import ContentItem

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 5000   # 全文上限（避免超出 AI token 限制）
MAX_REDDIT_COMMENTS = 3


class ContentEnricher:
    """在关键词预筛之后、AI 处理之前，丰富内容。"""

    def __init__(self, concurrency: int = 5, timeout: int = 15):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = timeout

    async def enrich(self, items: list[ContentItem]) -> list[ContentItem]:
        """批量丰富：RSS 抓全文，Reddit 抓评论，其他源跳过。"""
        if not items:
            return items

        tasks = []
        for item in items:
            if item.source == "rss":
                tasks.append(self._enrich_rss(item))
            elif item.source == "reddit":
                tasks.append(self._enrich_reddit(item))
            # YouTube / Twitter 暂不处理

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"全文提取异常[{i}]: {result}")

        enriched = sum(1 for it in items if it.full_text)
        logger.info(f"全文提取: {enriched}/{len(items)} 条成功")
        return items

    async def _enrich_rss(self, item: ContentItem):
        """下载文章 HTML → trafilatura 提取正文。"""
        if not item.url:
            return
        async with self.semaphore:
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.get(
                        item.url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; OPC-bot/1.0; +https://github.com/wsjxphz-png/opc-daily-opportunity-bot)",
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                        },
                    )
                    if resp.status_code != 200:
                        logger.debug(f"全文提取 HTTP {resp.status_code}: {item.url[:80]}")
                        return

                    html = resp.text
                    text = trafilatura.extract(
                        html,
                        include_comments=False,
                        include_tables=False,
                        no_fallback=True,
                        favor_precision=True,
                    )
                    if text and len(text) > 100:
                        item.full_text = text[:MAX_TEXT_LEN]
                        logger.debug(f"RSS 全文提取成功: {item.title[:50]}... ({len(item.full_text)} 字)")
                    else:
                        logger.debug(f"RSS 全文提取空: {item.url[:80]}")

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.debug(f"RSS 全文提取超时/连接失败: {item.url[:80]} — {type(e).__name__}")
            except Exception as e:
                logger.debug(f"RSS 全文提取失败: {item.url[:80]} — {e}")

    async def _enrich_reddit(self, item: ContentItem):
        """通过 Reddit JSON API 抓取帖子评论。"""
        if not item.url:
            return
        async with self.semaphore:
            try:
                # 从 URL 提取 post_id: /comments/{post_id}/
                match = re.search(r"/comments/([a-z0-9]+)/", item.url)
                if not match:
                    return
                post_id = match.group(1)

                # 从 source_name 提取子版块名: "r/Gumroad" → "Gumroad"
                sub = item.source_name.replace("r/", "").strip()

                json_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}.json"
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.get(
                        json_url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; OPC-bot/1.0)",
                            "Accept": "application/json",
                        },
                    )
                    if resp.status_code != 200:
                        logger.debug(f"Reddit 评论 HTTP {resp.status_code}: {json_url[:80]}")
                        return

                    data = resp.json()
                    if not isinstance(data, list) or len(data) < 2:
                        return

                    # 提取评论
                    comments_data = data[1]["data"]["children"]
                    top_comments = []
                    for child in comments_data:
                        if child["kind"] != "t1":
                            continue
                        body = child["data"].get("body", "")
                        if body and len(body) > 50 and not body.startswith(">") and not body.startswith("["):
                            # 清理 markdown
                            body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
                            body = re.sub(r"\n{2,}", "\n", body).strip()
                            top_comments.append(body[:800])
                        if len(top_comments) >= MAX_REDDIT_COMMENTS:
                            break

                    if top_comments:
                        comments_text = "\n\n---\n".join(top_comments)
                        # 把原标题+摘要和评论合并
                        post_content = f"{item.title}\n\n{item.summary}\n\n[Top Comments]\n{comments_text}"
                        item.full_text = post_content[:MAX_TEXT_LEN]
                        logger.debug(f"Reddit 评论提取成功: r/{sub} ({len(top_comments)} 条评论)")

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.debug(f"Reddit 评论超时: {item.url[:80]}")
            except Exception as e:
                logger.debug(f"Reddit 评论提取失败: {item.url[:80]} — {e}")
