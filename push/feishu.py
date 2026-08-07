"""
飞书推送模块 — 组装卡片消息并推送到群聊。

支持双板块：国内（中文源）+ 国际（英文源），各一板块。
"""

import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from sources.base import ContentItem

logger = logging.getLogger(__name__)

COLORS = {
    "blue": "blue",
    "red": "red",
    "green": "green",
    "yellow": "yellow",
    "purple": "purple",
    "turquoise": "turquoise",
}

DIFFICULTY_BADGE = {
    "零门槛": "🟢 零门槛",
    "需学习": "🟡 需学习",
    "有一定门槛": "🟠 有一定门槛",
}

SECTION_EMOJI = {"国内": "🇨🇳", "国际": "🌍"}


class FeishuPusher:
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

    # ================================================================
    # 入口：双板块推送
    # ================================================================

    async def push_dual_report(
        self,
        domestic: list[ContentItem],
        international: list[ContentItem],
        date_str: str,
    ) -> bool:
        """推送每日报告 — 国内 + 国际两个板块。"""
        if not self._enabled:
            logger.warning("飞书 webhook 未配置")
            return False
        if not domestic and not international:
            logger.info("无内容需要推送")
            return False

        total = len(domestic) + len(international)
        card = self._build_dual_card(domestic, international, date_str, total)
        return await self._send_card(card)

    # ================================================================
    # 双板块卡片组装
    # ================================================================

    def _build_dual_card(
        self,
        domestic: list[ContentItem],
        international: list[ContentItem],
        date_str: str,
        total: int,
    ) -> dict:
        header = {
            "title": {
                "tag": "plain_text",
                "content": f"🔔 OPC 一人公司 · 每日机会 | {date_str}",
            },
            "template": self.card_color,
        }

        elements = []

        # ---- 顶部统计 ----
        stats_parts = []
        if domestic:
            stats_parts.append(f"🇨🇳 国内 {len(domestic)} 条")
        else:
            stats_parts.append(f"🇨🇳 国内 0 条（中文源内容偏少，持续优化中）")
        if international:
            stats_parts.append(f"🌍 国际 {len(international)} 条")
        else:
            stats_parts.append(f"🌍 国际 0 条")
        stats_text = "  ·  ".join(stats_parts)

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📊 今日共 {total} 条机会**\n{stats_text}",
            },
        })
        elements.append({"tag": "hr"})

        # ---- 国内板块 ----
        if domestic:
            elements.extend(self._build_section("国内", domestic, "red"))
            if international:
                elements.append({"tag": "hr"})

        # ---- 国际板块 ----
        if international:
            elements.extend(self._build_section("国际", international, "blue"))

        # ---- 底部 ----
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"🤖 由 AI 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            }],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }

    # ================================================================
    # 板块内部：标题 + 条目
    # ================================================================

    def _build_section(
        self, label: str, items: list[ContentItem], color: str
    ) -> list[dict]:
        emoji = SECTION_EMOJI.get(label, "📌")
        elements = []

        # 板块标题
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{emoji} **{label}机会**",
            },
        })

        top_score = max(it.relevance_score for it in items) if items else 0

        for i, item in enumerate(items, 1):
            is_top = item.relevance_score >= top_score and top_score >= 0.7

            title_text = item.title
            if len(title_text) > 120:
                title_text = title_text[:120] + "..."

            ai_summary = item.ai_summary or ""
            opportunity = item.opportunity_hint or ""
            translation = item.translation or ""
            difficulty = item.difficulty or ""
            diff_badge = DIFFICULTY_BADGE.get(difficulty, "")
            source_label = item.source_name or item.source

            # 标题链接
            if item.url:
                title_line = f"**[{title_text}]({item.url})**"
            else:
                title_line = f"**{title_text}**"

            md_lines = []

            # 首选标记 + 难度
            if is_top and len(items) > 1:
                tag = f"⭐ 首选 · {diff_badge}" if diff_badge else "⭐ 首选"
                md_lines.append(tag)
            elif diff_badge:
                md_lines.append(diff_badge)

            md_lines.append(title_line)

            if ai_summary:
                md_lines.append(f"📖 {ai_summary}")

            if opportunity and "暂无" not in opportunity and "无" != opportunity.strip():
                md_lines.append(f"💡 怎么模仿：{opportunity}")

            if translation:
                md_lines.append(f"🌐 原标题：{translation[:120]}")

            md_lines.append(f"📎 来自「{source_label}」· {self._time_str(item.published)}")

            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(md_lines),
                },
            })

            if i < len(items):
                elements.append({"tag": "hr"})

        return elements

    # ================================================================
    # 工具
    # ================================================================

    async def _send_card(self, card: dict) -> bool:
        payload = {"msg_type": "interactive", "card": card}
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
