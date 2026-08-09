"""
飞书推送模块 — 组装卡片消息并推送到群聊。

支持双板块：国内（中文源）+ 国际（英文源），按当天内容质量动态配比，不硬性规定各 5 条。
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

QUALITY_ICON = {
    "⭐": "⭐ 高价值",
    "⚠️": "⚠️ 注意甄别",
}

CODE_DEPENDENCY_LABEL = {
    1: "🟢 无需代码",
    2: "🟢 只需工具",
    3: "🟡 需配工具",
    4: "🟠 需写代码",
    5: "🔴 纯编程",
}

AUTHENTICITY_LABEL = {
    5: "★★★★★ 真金白银",
    4: "★★★★☆ 可信",
    3: "★★★☆☆ 可参考",
    2: "★★☆☆☆ 有水分",
    1: "★☆☆☆☆ 卖铲子",
}

SECTION_EMOJI = {"国内": "🇨🇳", "国际": "🌍"}

REPLICABILITY_LABEL = {
    5: "⭐⭐⭐⭐⭐ 极易照做",
    4: "⭐⭐⭐⭐ 较易模仿",
    3: "⭐⭐⭐ 需学一下",
    2: "⭐⭐ 门槛偏高",
    1: "⭐ 基本做不到",
}

TECH_BARRIER_LABEL = {
    "无": "🟢 无需代码",
    "低": "🟢 可用无代码工具",
    "中": "🟡 需配工具/半技术",
    "高": "🔴 需写代码/开发",
}

DOABLE_LABEL = {
    "能": "✅ 你能照做",
    "降级可做": "🟡 降级可做（用无代码工具替代）",
    "做不到": "⛔ 你做不到（需写代码）",
}

# 来源平台中文名（仅覆盖常见平台，减少英文观感；其余保留原样）
SOURCE_LABEL_MAP = {
    "twitter": "推特",
    "x": "X（推特）",
    "reddit": "Reddit（海外论坛）",
    "youtube": "YouTube",
    "bilibili": "B站",
    "weibo": "微博",
    "xiaohongshu": "小红书",
    "zhihu": "知乎",
    "substack": "Substack（邮件订阅）",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "linkedin": "领英",
}


class FeishuPusher:
    def __init__(self, webhook_url: str, card_color: str = "blue", batch_size: int = 8):
        self.webhook_url = webhook_url
        self.card_color = COLORS.get(card_color, "blue")
        self._batch_size = max(1, int(batch_size))
        self._enabled = bool(
            webhook_url
            and "open.feishu.cn" in webhook_url
            and "YOUR_WEBHOOK_TOKEN" not in webhook_url
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ================================================================
    # 入口：模块2 赚钱机会挖掘（国内 + 国际双板块）
    # ================================================================

    async def push_opportunities(
        self,
        domestic: list[ContentItem],
        international: list[ContentItem],
        date_str: str,
    ) -> bool:
        """模块2：推送 OPC赚钱机会挖掘日报 — 国内 + 国际两个板块。

        命中硬过滤的优质内容不封顶；当单板块条数超过批次上限时，自动拆成多张卡
        分批推送（每张卡标注「第N批/共M批」），避免一条消息过长被截断。
        """
        if not self._enabled:
            logger.warning("飞书 webhook 未配置")
            return False
        if not domestic and not international:
            logger.info("无机会内容需要推送")
            return False

        total = len(domestic) + len(international)
        batch = self._batch_size

        # 总量未超一批：保持单卡（旧样式，国内+国际同卡）
        if total <= batch:
            card = self._build_dual_card(domestic, international, date_str, total)
            return await self._send_card(card)

        # 超批：国内 / 国际分别切块，每块一张卡，顺序推送
        cards = []
        dom_chunks = (
            [domestic[i : i + batch] for i in range(0, len(domestic), batch)]
            if domestic else []
        )
        intl_chunks = (
            [international[i : i + batch] for i in range(0, len(international), batch)]
            if international else []
        )
        chunks = [("国内", c, "red") for c in dom_chunks] + [
            ("国际", c, "blue") for c in intl_chunks
        ]
        n = len(chunks)
        for idx, (label, items, color) in enumerate(chunks, 1):
            cards.append(self._build_section_card(label, items, color, date_str, idx, n, total))

        ok_all = True
        for card in cards:
            ok = await self._send_card(card)
            ok_all = ok_all and ok
        logger.info(f"模块2 分批推送: 共 {n} 张卡（每批 {batch} 条，总计 {total} 条）")
        return ok_all

    def _build_section_card(
        self, label: str, items: list[ContentItem], color: str,
        date_str: str, batch_no: int, total_batches: int, grand_total: int,
    ) -> dict:
        """构建「单板块单批次」卡片（超批时用于分批推送）。"""
        header = {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"💡 OPC赚钱机会挖掘日报 | {date_str}"
                    + (f"  (第{batch_no}/{total_batches}批)" if total_batches > 1 else "")
                ),
            },
            "template": self.card_color,
        }
        elements = []
        stats = []
        emoji = SECTION_EMOJI.get(label, "📌")
        stats.append(f"{emoji} {label} {len(items)} 条")
        if total_batches > 1:
            stats.append(f"第{batch_no}/{total_batches}批（共 {grand_total} 条机会，分批推送）")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**📊 " + "  ·  ".join(stats) + "**"},
        })
        elements.append({"tag": "hr"})
        elements.extend(self._build_section(label, items, color))
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"🤖 由 AI 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            }],
        })
        return {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}

    # 兼容别名（历史方法名）
    async def push_dual_report(self, domestic, international, date_str) -> bool:
        return await self.push_opportunities(domestic, international, date_str)

    # ================================================================
    # 内容源侦察兵：新挖掘到的可追更内容源（公众号）
    # ================================================================

    async def push_scouted_sources(
        self,
        sources: list,
        date_str: str,
    ) -> bool:
        """推送「📡 新挖掘内容源」卡片 —— 侦察兵当日发现并加入白名单的公众号。

        sources 元素可为 ScoutedSource 或 dict（兼容测试），需含 name/platform/score/reason。
        """
        if not self._enabled:
            logger.warning("飞书 webhook 未配置")
            return False
        if not sources:
            logger.info("无新挖掘内容源，跳过推送")
            return False

        header = {
            "title": {
                "tag": "plain_text",
                "content": f"📡 OPC赚钱机会挖掘日报 · 新挖掘内容源 | {date_str}",
            },
            "template": "purple",
        }
        elements = []
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**📡 今日侦察兵新挖掘 {len(sources)} 个内容源**\n"
                    "已自动加入白名单，从明天起每天追更它们的文章"
                ),
            },
        })
        elements.append({"tag": "hr"})

        for i, s in enumerate(sources, 1):
            name = getattr(s, "name", None)
            platform = getattr(s, "platform", None)
            score = getattr(s, "score", None)
            reason = getattr(s, "reason", None)
            if name is None and isinstance(s, dict):
                name = s.get("name", "")
                platform = s.get("platform", "weixin")
                score = s.get("score", 0)
                reason = s.get("reason", "")
            plat_label = "公众号" if platform == "weixin" else (platform or "内容源")
            score = score or 0
            md = [
                f"**{i}. {name}**（{plat_label} · 质量 {score}/10）",
            ]
            if reason:
                md.append(f"> {reason}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(md)},
            })
            if i < len(sources):
                elements.append({"tag": "hr"})

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"🤖 由 AI 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            }],
        })
        card = {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }
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
                "content": f"💡 OPC赚钱机会挖掘日报 | {date_str}",
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

            original_title = item.title or ""
            translation = item.translation or ""  # 英文标题的中文翻译（国内源为空串）

            # 优先用「中文翻译」做标题，英文原标题降级成脚注，减少英文观感
            display_title = translation if translation else original_title
            if len(display_title) > 120:
                display_title = display_title[:120] + "..."
            show_original = bool(original_title) and bool(translation) and original_title != translation

            ai_summary = item.ai_summary or ""
            opportunity = item.opportunity_hint or ""
            translation = item.translation or ""
            difficulty = item.difficulty or ""
            diff_badge = DIFFICULTY_BADGE.get(difficulty, "")
            quality_flag = getattr(item, "quality_flag", "") or ""
            quality_label = QUALITY_ICON.get(quality_flag, "")
            verdict = getattr(item, "verdict", "") or ""
            code_dep = getattr(item, "code_dependency", 0) or 0
            authenticity = getattr(item, "authenticity", 0) or 0
            practical_steps = getattr(item, "practical_steps", "") or ""
            source_label = item.source_name or item.source
            # 平台级来源名汉化（如 twitter → 推特）
            if source_label:
                src_key = source_label.split("/")[-1].split(".")[0].lower()
                source_label = SOURCE_LABEL_MAP.get(src_key, source_label)

            # 代码依赖度标签
            code_label = CODE_DEPENDENCY_LABEL.get(code_dep, "")
            # 真实性标签
            auth_label = AUTHENTICITY_LABEL.get(authenticity, "")

            # 标题链接（用中文翻译做主标题）
            if item.url:
                title_line = f"**[{display_title}]({item.url})**"
            else:
                title_line = f"**{display_title}**"

            md_lines = []

            # 首选标记 + 判定结论 + 难度 + 质量标记
            tags = []
            if is_top and len(items) > 1:
                tags.append("⭐ 首选")
            if verdict:
                verdict_emoji = "✅" if "真机会" in verdict else "🚫"
                tags.append(f"{verdict_emoji} {verdict}")
            if diff_badge:
                tags.append(diff_badge)
            if quality_label:
                tags.append(quality_label)
            if tags:
                md_lines.append(" · ".join(tags))

            md_lines.append(title_line)

            # 评估分数行
            score_parts = []
            if code_label:
                score_parts.append(code_label)
            if auth_label:
                score_parts.append(auth_label)
            score_parts.append(f"综合 {item.relevance_score:.2f}")
            md_lines.append(" · ".join(score_parts))

            if ai_summary:
                md_lines.append(f"📖 {ai_summary}")

            if practical_steps:
                md_lines.append(f"🔧 实操步骤：\n{practical_steps}")
            elif opportunity and "暂无" not in opportunity and "无" != opportunity.strip():
                md_lines.append(f"💡 怎么模仿：{opportunity}")

            if show_original:
                md_lines.append(f"🌐 英文原标题：{original_title[:120]}")

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
    # 操盘手拆解卡 + 新发现预警
    # ================================================================

    def _build_teardown_section(self, td: dict) -> list[dict]:
        """把一张结构化拆解渲染成卡片元素。"""
        elements = []
        name = td.get("operator_name", "")
        region = td.get("region", "")
        region_emoji = "🇨🇳" if region == "国内" else "🌍"
        rep = td.get("replicability", 0) or 0
        rep_label = REPLICABILITY_LABEL.get(rep, "")
        tb = td.get("tech_barrier", "")
        tb_label = TECH_BARRIER_LABEL.get(tb, "")
        doable = td.get("doable", "")
        doable_label = DOABLE_LABEL.get(doable, doable)

        is_revisit = td.get("is_revisit")
        title_prefix = "🔄 操盘手动态更新" if is_revisit else "🔍 操盘手拆解"
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"# {title_prefix} · {region_emoji} {name}",
            },
        })
        if is_revisit:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "> 基于上次拆解后的新动态补充（新业务 / 新边界 / 新赚钱方式）",
                },
            })

        fields = [
            ("👤 人物", td.get("who", "")),
            ("📦 交付物", td.get("deliverable", "")),
            ("💰 商业模式", td.get("business_model", "")),
            ("🎯 获客方式", td.get("acquisition", "")),
            ("🧰 工具链", td.get("stack", "")),
            ("🔧 技术门槛", tb_label or "（未标注）"),
            ("🙋 你能否照做", doable_label or "（未标注）"),
            ("🚀 模仿第一步", td.get("first_step", "")),
            ("⚠️ 风险 / 卖铲子", td.get("red_flag", "")),
            ("✅ 最该抄的作业", td.get("learn", "")),
        ]
        for label, val in fields:
            if val:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{label}**：{val}",
                    },
                })

        meta = []
        if rep_label:
            meta.append(rep_label)
        sig = td.get("signals_used", 0) or 0
        meta.append(f"基于 {sig} 条新鲜信号")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": " · ".join(meta)},
        })
        return elements

    def _build_discovery_alert(self, d: dict) -> list[dict]:
        region = d.get("region", "国际")
        region_emoji = "🇨🇳" if region == "国内" else "🌍"
        name = d.get("name", "")
        handle = d.get("handle", "")
        handle_line = f"（{handle}）" if handle else ""
        highlight = d.get("highlight", "")
        tb = d.get("tech_barrier", "")
        tb_label = TECH_BARRIER_LABEL.get(tb, "")
        tb_line = f"  ·  {tb_label}" if tb_label else ""
        return [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"🆕 **{region_emoji} 发现新操盘手：{name}**{handle_line}{tb_line}\n"
                    f"> {highlight}"
                ),
            },
        }]

    async def push_teardowns(
        self,
        teardowns: list[dict],
        discovered: list[dict],
        date_str: str,
    ) -> bool:
        """推送拆解日报：深度拆解卡 + 新操盘手预警。

        当拆解卡数量超过批次上限时，自动拆成多张卡分批推送（标注「第N批/共M批」）。
        新发现预警统一附在最后一批（或唯一一批）卡片里。
        """
        if not self._enabled:
            logger.warning("飞书 webhook 未配置")
            return False
        if not teardowns and not discovered:
            logger.info("无拆解/发现内容，跳过推送")
            return False

        batch = self._batch_size
        td_chunks = (
            [teardowns[i : i + batch] for i in range(0, len(teardowns), batch)]
            if teardowns else []
        )
        n = len(td_chunks)
        ok_all = True

        if td_chunks:
            for idx, chunk in enumerate(td_chunks, 1):
                # 新发现预警只在最后一批（或唯一一批）出现
                disc = discovered if idx == n else []
                card = self._build_teardown_card(chunk, disc, date_str, idx, n)
                ok = await self._send_card(card)
                ok_all = ok_all and ok
        else:
            # 没有拆解卡但有新发现（少见）：单独一张卡
            card = self._build_teardown_card([], discovered, date_str, 1, 1)
            ok = await self._send_card(card)
            ok_all = ok_all and ok

        if n > 1:
            logger.info(f"模块1 分批推送: 共 {n} 张卡（每批 {batch} 张拆解卡）")
        return ok_all

    def _build_teardown_card(
        self,
        teardowns: list[dict],
        discovered: list[dict],
        date_str: str,
        batch_no: int,
        total_batches: int,
    ) -> dict:
        """构建一张拆解日报卡（可能含批号）。"""
        header = {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"🔍 OPC赚钱机会挖掘日报 · 操盘手拆解 | {date_str}"
                    + (f"  (第{batch_no}/{total_batches}批)" if total_batches > 1 else "")
                ),
            },
            "template": self.card_color,
        }
        elements = []

        n_new = sum(1 for t in teardowns if not t.get("is_revisit"))
        n_rev = sum(1 for t in teardowns if t.get("is_revisit"))
        stats = []
        if n_new:
            stats.append(f"📑 今日拆解 {n_new} 人")
        if n_rev:
            stats.append(f"🔄 动态更新 {n_rev} 人")
        if discovered:
            stats.append(f"🆕 新发现 {len(discovered)} 人")
        if total_batches > 1:
            stats.append(f"第{batch_no}/{total_batches}批")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**" + "  ·  ".join(stats) + "**"},
        })
        elements.append({"tag": "hr"})

        for i, td in enumerate(teardowns):
            elements.extend(self._build_teardown_section(td))
            if i < len(teardowns) - 1:
                elements.append({"tag": "hr"})

        if discovered:
            if teardowns:
                elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**🆕 新操盘手雷达**"},
            })
            for d in discovered:
                elements.extend(self._build_discovery_alert(d))
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
    # 工具
    # ================================================================

    async def _send_card(self, card: dict) -> bool:
        payload = {"msg_type": "interactive", "card": card}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
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
