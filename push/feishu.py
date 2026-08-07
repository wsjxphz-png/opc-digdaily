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
        source_stats = {}
        for item in items:
            s = item.source
            source_stats[s] = source_stats.get(s, 0) + 1

        stats_text = " · ".join(
            f"{SOURCE_EMOJI.get(k, '📌')} {v} 条 {k}"
            for k, v in source_stats.items()
        )

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📊 今日共 {len(items)} 条高价值信息**\n{stats_text}",
                },
            },
            {"tag": "hr"},
        ]

        # ===== 每条内容卡片 =====
        for i, item in enumerate(items, 1):
            emoji = SOURCE_EMOJI.get(item.source, "📌")
            source_tag = item.source_name or item.source

            # 标题行（最多 100 字）
            title_text = item.title
            if len(title_text) > 100:
                title_text = title_text[:100] + "..."

            # AI 总结
            ai_summary = item.ai_summary or ""
            opportunity = item.opportunity_hint or ""

            # 翻译（如果有）
            translation = item.translation or ""

            # 构建 markdown 内容 — 标题必须是可点击链接
            if item.url:
                title_line = f"**{emoji} [{title_text}]({item.url})**"
            else:
                title_line = f"**{emoji} {title_text}**"

            md_lines = [
                title_line,
                f"来源：{source_tag}  |  {self._time_str(item.published)}",
            ]
            if translation:
                md_lines.append(f"🌐 {translation[:120]}")
            if ai_summary:
                md_lines.append(f"💡 {ai_summary}")
            if opportunity and "暂无" not in opportunity and "无" != opportunity.strip():
                md_lines.append(f"💰 机会：{opportunity}")

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
