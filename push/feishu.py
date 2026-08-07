"""
飞书推送模块 — 组装卡片消息并推送到群聊。

使用飞书消息卡片 (Message Card) 格式，支持富文本、图片、按钮等。
参考: https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-components
"""

import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from sources.base import ContentItem

logger = logging.getLogger(__name__)

# 卡片颜色常量
COLORS = {
    "blue": "blue",
    "red": "red",
    "green": "green",
    "yellow": "yellow",
    "purple": "purple",
    "turquoise": "turquoise",
}

SOURCE_EMOJI = {
    "youtube": "▶️",
    "rss": "📡",
    "reddit": "🤖",
    "twitter": "🐦",
    "wechat": "💬",
}

DIFFICULTY_BADGE = {
    "零门槛": "🟢 零门槛",
    "需学习": "🟡 需学习",
    "有一定门槛": "🟠 有一定门槛",
}


class FeishuPusher:
    """飞书卡片推送器。"""

    def __init__(self, webhook_url: str, card_color: str = "blue"):
        self.webhook_url = webhook_url
        self.card_color = COLORS.get(card_color, "blue")
        self._enabled = bool(
            webhook_url
            and "open.feishu.cn" in webhook_url
            and "YOUR_WEBHOOK_TOKEN" not in webhook_url
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def push_daily_report(
        self, items: list[ContentItem], date_str: str
    ) -> bool:
        """推送每日报告 — 多条内容用单个卡片消息。"""
        if not self._enabled:
            logger.warning("飞书 webhook 未配置")
            return False
        if not items:
            logger.info("无内容需要推送")
            return False

        card = self._build_card(items, date_str)
        return await self._send_card(card)

    def _build_card(self, items: list[ContentItem], date_str: str) -> dict:
        """组装飞书消息卡片 JSON。"""

        # ===== 卡片头部 =====
        header = {
            "title": {
                "tag": "plain_text",
                "content": f"🔔 OPC 一人公司 · 每日机会挖掘 | {date_str}",
            },
            "template": self.card_color,
        }

        # ===== 总览统计 =====
        # 来源：用实际的源名称（不是 "rss"/"reddit" 这种笼统标签）
        source_names: list[str] = []
        for item in items:
            name = item.source_name or item.source
            if name and name not in source_names:
                source_names.append(name)

        stats_parts = []
        # 难度分布
        easy = sum(1 for it in items if "零门槛" in (it.difficulty or ""))
        learn = sum(1 for it in items if "需学习" in (it.difficulty or ""))
        hard = sum(1 for it in items if "有一定门槛" in (it.difficulty or ""))
        diff_parts = []
        if easy:
            diff_parts.append(f"🟢{easy}")
        if learn:
            diff_parts.append(f"🟡{learn}")
        if hard:
            diff_parts.append(f"🟠{hard}")
        if diff_parts:
            stats_parts.append(f"难度：{' '.join(diff_parts)}")
        if source_names:
            stats_parts.append(f"来源：{' · '.join(source_names[:4])}")
            if len(source_names) > 4:
                stats_parts[-1] += f" 等{len(source_names)}个"

        stats_text = "\n".join(stats_parts) if stats_parts else f"今日共 {len(items)} 条"

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📊 今日共 {len(items)} 条机会**\n{stats_text}",
                },
            },
            {"tag": "hr"},
        ]

        # ===== 每条内容卡片 =====
        # 找最高分作为"今日首选"
        top_score = max(it.relevance_score for it in items) if items else 0

        for i, item in enumerate(items, 1):
            is_top_pick = item.relevance_score >= top_score and top_score >= 0.7

            # 标题（最多 120 字）
            title_text = item.title
            if len(title_text) > 120:
                title_text = title_text[:120] + "..."

            # AI 总结和机会
            ai_summary = item.ai_summary or ""
            opportunity = item.opportunity_hint or ""

            # 翻译
            translation = item.translation or ""

            # 难度标签
            difficulty = item.difficulty or ""
            diff_badge = DIFFICULTY_BADGE.get(difficulty, "")

            # 来源显示：用真实源名称，而非 "rss"/"reddit"
            source_label = item.source_name or item.source

            # 构建 markdown
            if item.url:
                title_line = f"**[{title_text}]({item.url})**"
            else:
                title_line = f"**{title_text}**"

            md_lines = []

            # 今日首选标记
            if is_top_pick and len(items) > 1:
                md_lines.append(f"⭐ **今日首选** · {diff_badge}" if diff_badge else "⭐ **今日首选**")
            elif diff_badge:
                md_lines.append(diff_badge)

            md_lines.append(title_line)

            # 文章大意
            if ai_summary:
                md_lines.append(f"📖 **文章大意**：{ai_summary}")

            # 机会提示
            if opportunity and "暂无" not in opportunity and "无" != opportunity.strip():
                md_lines.append(f"💡 **怎么模仿**：{opportunity}")

            # 翻译
            if translation:
                md_lines.append(f"🌐 原标题：{translation[:120]}")

            # 来源
            md_lines.append(f"📎 来自「{source_label}」· {self._time_str(item.published)}")

            elem = {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(md_lines),
                },
            }

            elements.append(elem)

            # 分隔线 (非最后一条)
            if i < len(items):
                elements.append({"tag": "hr"})

        # ===== 卡片底部 =====
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"🤖 由 AI 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                }
            ],
        })

        card = {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }
        return card

    async def _send_card(self, card: dict) -> bool:
        """发送卡片到飞书群。"""
        payload = {
            "msg_type": "interactive",
            "card": card,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") == 0:
                    logger.info("飞书推送成功")
                    return True
                else:
                    logger.error(f"飞书推送失败: {result}")
                    return False
        except Exception as e:
            logger.error(f"飞书推送异常: {e}")
            return False

    @staticmethod
    def _time_str(dt: Optional[datetime]) -> str:
        if not dt:
            return "未知"
        return dt.strftime("%m-%d %H:%M")
