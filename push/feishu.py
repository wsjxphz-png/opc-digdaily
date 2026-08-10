"""
飞书推送模块 — 组装卡片消息并推送到群聊。

支持双板块：国内（中文源）+ 国际（英文源），按当天内容质量动态配比，不硬性规定各 5 条。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from sources.base import ContentItem
from feedback import build_feedback_urls

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


def _startup_badge(idx: int) -> str:
    """把 1-10 的「适合你启动指数」渲染成一眼可读的标签。"""
    if idx >= 9:
        return f"🔥 适合你启动 {idx}/10（今天最该抄）"
    if idx >= 7:
        return f"🎯 适合你启动 {idx}/10（值得动手）"
    if idx >= 5:
        return f"🟡 适合你启动 {idx}/10（可以看看）"
    return f"⚪ 适合你启动 {idx}/10（参考为主）"


def _startup_suffix(idx: int) -> str:
    """启动指数对应的括号后缀（配合其他排版使用）。"""
    if idx >= 9:
        return "（今天最该抄）"
    if idx >= 7:
        return "（值得动手）"
    if idx >= 5:
        return "（可以看看）"
    return "（参考为主）"


class FeishuPusher:
    def __init__(
        self,
        webhook_url: str,
        card_color: str = "blue",
        batch_size: int = 30,
        feedback_repo: str = "",
        feedback_enabled: bool = False,
        dry_run: bool = False,
        ai_api_base: str = "",
        ai_api_key: str = "",
        ai_model: str = "",
    ):
        self.webhook_url = webhook_url
        self.card_color = COLORS.get(card_color, "blue")
        self._batch_size = max(1, int(batch_size))
        self.feedback_repo = (feedback_repo or "").strip()
        self.feedback_enabled = bool(feedback_enabled and self.feedback_repo)
        # 演练模式：完整跑通流程并渲染卡片，但绝不真的发到群里
        self.dry_run = bool(dry_run)
        self._ai_api_base = (ai_api_base or "").strip()
        self._ai_api_key = (ai_api_key or "").strip()
        self._ai_model = (ai_model or "").strip()
        self._enabled = bool(
            webhook_url
            and "open.feishu.cn" in webhook_url
            and "YOUR_WEBHOOK_TOKEN" not in webhook_url
        )
        # 演练时即使没配 webhook 也要让流程往下走，才能看到卡片长什么样
        if self.dry_run:
            self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ================================================================
    # 英文标题翻译：AI 批量任务里常被跳过，这里做独立补刀
    # ================================================================
    async def _translate_titles(self, items: list[ContentItem]) -> list[ContentItem]:
        """对无翻译的英文标题做独立批量翻译，只调一次 API。"""
        need = [(i, it) for i, it in enumerate(items) if it.title and not it.translation]
        if not need or not self._ai_api_key:
            return items

        # 筛出主要含英文的标题
        def _mostly_ascii(s: str) -> bool:
            return sum(1 for c in s if ord(c) < 128) / max(len(s), 1) > 0.6

        to_translate = [(idx, it.title.strip()) for idx, it in need if _mostly_ascii(it.title)]
        if not to_translate:
            return items

        titles = [t for _, t in to_translate]
        prompt = (
            "将以下英文文章标题翻译成中文。每行一个翻译，保持顺序，不要编号，不要原文。\n\n"
            + "\n".join(titles)
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._ai_api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._ai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._ai_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": min(500, len(titles) * 40),
                        "temperature": 0.1,
                    },
                )
                if resp.status_code == 200:
                    body = resp.json()
                    text = body["choices"][0]["message"]["content"].strip()
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    if len(lines) == len(titles):
                        for (idx, _), zh in zip(to_translate, lines):
                            items[idx].translation = zh
                        logger.info(f"独立翻译完成：{len(titles)} 条英文标题 → 中文")
                    else:
                        logger.warning(f"翻译返回行数不匹配 ({len(lines)} vs {len(titles)})，跳过补刀")
                else:
                    logger.warning(f"翻译 API 返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"独立翻译失败: {e}")
        return items

    # ================================================================
    # 入口：模块2 赚钱机会挖掘（国内 + 国际双板块）
    # ================================================================

    async def push_opportunities(
        self,
        domestic: list[ContentItem],
        international: list[ContentItem],
        date_str: str,
        recurring: Optional[list[dict]] = None,
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

        # ---- 补刀翻译：国际条目若 AI 没给翻译，这里独立翻一次 ----
        if international:
            international = await self._translate_titles(international)
        if domestic:
            domestic = await self._translate_titles(domestic)

        total = len(domestic) + len(international)
        batch = self._batch_size

        # 总量未超一批：保持单卡（旧样式，国内+国际同卡）
        if total <= batch:
            card = self._build_dual_card(domestic, international, date_str, total, recurring)
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
            cards.append(self._build_section_card(
                label, items, color, date_str, idx, n, total,
                recurring if idx == 1 else None,
            ))

        ok_all = True
        for card in cards:
            ok = await self._send_card(card)
            ok_all = ok_all and ok
        logger.info(f"模块2 分批推送: 共 {n} 张卡（每批 {batch} 条，总计 {total} 条）")
        return ok_all

    def _build_recurring_block(self, recurring: Optional[list[dict]]) -> list[dict]:
        """「本周风向」：跨天机会库里近期反复出现的主题（多来源印证 = 强信号）。"""
        if not recurring:
            return []
        lines = ["**🔁 本周反复出现的方向**（同一件事被不同来源、在不同天里反复讲到，可信度更高）"]
        for r in recurring:
            lines.append(
                f"· {r.get('topic', '')} — 第 {r.get('times', 1)} 次出现"
                f"，{r.get('sources', 1)} 个来源印证"
                f"（首次 {r.get('first_seen', '')}）"
            )
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
        ]

    def _build_section_card(
        self, label: str, items: list[ContentItem], color: str,
        date_str: str, batch_no: int, total_batches: int, grand_total: int,
        recurring: Optional[list[dict]] = None,
    ) -> dict:
        """构建「单板块单批次」卡片（超批时用于分批推送）。"""
        header = {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"💡 OPC赚钱机会 | {date_str}"
                    + (f" · {label} {len(items)} 条" if label else "")
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
        elements.extend(self._build_recurring_block(recurring))
        elements.extend(self._build_section(label, items, color))
        elements.append({"tag": "hr"})
        elements.append(self._footer_note())
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
        recurring: Optional[list[dict]] = None,
    ) -> dict:
        header = {
            "title": {
                "tag": "plain_text",
                "content": f"💡 OPC赚钱机会 | {date_str} · 🇨🇳{len(domestic)}条 · 🌍{len(international)}条",
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

        # ---- 📋 目录 ----
        all_items = [
            ("🇨🇳", "国内", dom) for dom in [domestic] if dom
        ] + [
            ("🌍", "国际", intl) for intl in [international] if intl
        ]
        toc_lines = ["**📋 今日目录**"]
        idx = 1
        for flag, region, items in all_items:
            toc_lines.append(f"\n{flag} {region}：")
            for item in items:
                si = getattr(item, "startup_index", 0) or 0
                tl_text = item.translation or item.title or ""
                if len(tl_text) > 35:
                    tl_text = tl_text[:32] + "..."
                badge = f"🎯{si}/10"
                toc_lines.append(f"  {idx}. {tl_text}  {badge}")
                idx += 1
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(toc_lines)},
        })
        elements.append({"tag": "hr"})

        # ---- 本周风向（跨天机会库）----
        elements.extend(self._build_recurring_block(recurring))

        # ---- 国内板块 ----
        next_idx = 1
        if domestic:
            elements.extend(self._build_section("国内", domestic, "red", next_idx))
            next_idx += len(domestic)
            if international:
                elements.append({"tag": "hr"})

        # ---- 国际板块 ----
        if international:
            elements.extend(self._build_section("国际", international, "blue", next_idx))

        # ---- 底部 ----
        elements.append({"tag": "hr"})
        elements.append(self._footer_note())

        return {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }

    def _footer_note(self) -> dict:
        tips = [f"🤖 由 AI 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
        tips.append("分数说明：适合你启动指数由公式自动计算")
        if self.feedback_enabled:
            tips.append(
                "点「想做 / 没兴趣」会打开一个已填好的反馈页，再点一下提交即可；"
                "明天的推送会据此调整排序"
            )
        return {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "　|　".join(tips)}],
        }

    # ================================================================
    # 板块内部：标题 + 条目
    # ================================================================
    def _parse_practical_steps(self, steps_text: str) -> dict:
        """从 AI 生成的实操步骤字符串中提取三部分。"""
        result = {"deliverable": "", "acquisition": "", "tools": ""}
        if not steps_text:
            return result
        key_map = {"交付物": "deliverable", "前5个客户": "acquisition", "工具链": "tools"}
        for line in steps_text.split("\n"):
            line = line.strip()
            for prefix, key in key_map.items():
                if prefix in line:
                    val = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                    result[key] = val
                    break
        return result

    def _build_section(
        self, label: str, items: list[ContentItem], color: str, start_index: int = 1
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

        top_score = (
            max((getattr(it, "startup_index", 0) or 0) for it in items) if items else 0
        )

        for j, item in enumerate(items):
            i = start_index + j  # 全卡序号
            startup_index = getattr(item, "startup_index", 0) or 0
            is_top = startup_index >= top_score and top_score >= 7

            original_title = item.title or ""
            translation = item.translation or ""

            # 优先中文翻译
            display_title = translation if translation else original_title
            if not translation and original_title:
                ascii_count = sum(1 for c in original_title if ord(c) < 128)
                if ascii_count / max(len(original_title), 1) > 0.6:
                    display_title = f"英文｜{original_title[:110]}" if len(original_title) > 110 else f"英文｜{original_title}"
                elif len(display_title) > 120:
                    display_title = display_title[:120] + "..."
            elif len(display_title) > 120:
                display_title = display_title[:120] + "..."

            ai_summary = item.ai_summary or ""
            difficulty = item.difficulty or ""
            diff_badge = DIFFICULTY_BADGE.get(difficulty, "")
            code_dep = getattr(item, "code_dependency", 0) or 0
            code_label = CODE_DEPENDENCY_LABEL.get(code_dep, "")
            authenticity = getattr(item, "authenticity", 0) or 0
            auth_label = AUTHENTICITY_LABEL.get(authenticity, "")
            practical_steps = getattr(item, "practical_steps", "") or ""
            source_label = item.source_name or item.source
            if source_label:
                src_key = source_label.split("/")[-1].split(".")[0].lower()
                source_label = SOURCE_LABEL_MAP.get(src_key, source_label)

            # 解析实操步骤三部分
            steps = self._parse_practical_steps(practical_steps)
            tpl = getattr(item, "copy_template", None) or {}

            # 标题链接
            title_line = f"**[{display_title}]({item.url})**" if item.url else f"**{display_title}**"

            md_lines = []

            # --- 编号 + 标题 + 总分 ---
            tags = []
            if is_top and len(items) > 1:
                tags.append("⭐ 首选")
            if diff_badge:
                tags.append(diff_badge)
            tag_str = " · ".join(tags)
            header_line = f"**{i}. {display_title}**" + (f"  （{tag_str}）" if tag_str else "")
            if item.url:
                header_line = f"**{i}. [{display_title}]({item.url})**" + (f"  （{tag_str}）" if tag_str else "")
            md_lines.append(header_line)
            if startup_index:
                md_lines.append(f"🎯 适合你启动 **{startup_index}/10**{_startup_suffix(startup_index)}")

            # --- 摘要（包含💭判断，即风险分析）---
            if ai_summary:
                md_lines.append(f"📖 {ai_summary}")

            # --- 做什么产品 / 怎么获客 / 需要工具 ---
            if steps["deliverable"]:
                md_lines.append(f"📦 做什么产品：{steps['deliverable']}")
            elif tpl.get("what"):
                md_lines.append(f"📦 做什么产品：{tpl['what']}")
            if steps["acquisition"]:
                md_lines.append(f"📣 怎么获客：{steps['acquisition']}")
            if steps["tools"]:
                md_lines.append(f"🔧 需要工具：{steps['tools']}")

            # --- 技术门槛 ---
            if code_label:
                md_lines.append(f"🚪 技术门槛：{code_label}")

            # --- 你该怎么抄 ---
            if tpl:
                md_lines.append(f"\n👣 **你该怎么抄**")
                if tpl.get("who"):
                    md_lines.append(f"· 卖给谁：{tpl['who']}")
                if tpl.get("first_step"):
                    md_lines.append(f"· 第一步：{tpl['first_step']}")
                if tpl.get("first_prompt"):
                    md_lines.append(f"· 让 AI 帮你一句话：\n> {tpl['first_prompt']}")
                if tpl.get("cost"):
                    md_lines.append(f"· 花了多长时间、多少钱：{tpl['cost']}")

            # --- 可信度 / 重复出现 ---
            repeat = getattr(item, "repeat_count", 0) or 0
            corro = getattr(item, "corroborations", 0) or 0
            credit_parts = []
            if auth_label:
                credit_parts.append(auth_label)
            if repeat >= 2:
                credit_parts.append(f"🔁 第 {repeat} 次出现")
            if corro >= 2:
                credit_parts.append(f"✔️ {corro} 个来源印证")
            if credit_parts:
                md_lines.append(" · ".join(credit_parts))

            # --- 来源 ---
            published_str = self._time_str(item.published) if item.published else ""
            src_line = f"📎 来自「{source_label}」" + (f" · {published_str}" if published_str else "")
            md_lines.append(src_line)

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(md_lines)},
            })

            # ---- 反馈按钮 ----
            fb = self._build_feedback_actions(item, display_title)
            if fb:
                elements.append(fb)

            if j < len(items) - 1:
                elements.append({"tag": "hr"})

        return elements

    def _build_feedback_actions(self, item: ContentItem, title: str) -> Optional[dict]:
        """两个反馈按钮：点击打开已预填的 GitHub Issue 页面，提交即完成反馈。

        群自定义机器人 webhook 是单向的，收不到 callback 事件；用 url 按钮
        把反馈落到 GitHub Issue，次日运行时由 feedback.py 读回并调整权重。
        """
        if not self.feedback_enabled:
            return None
        key = getattr(item, "topic_key", "") or ""
        if not key:
            return None
        like_url, dislike_url = build_feedback_urls(self.feedback_repo, key, title)
        return {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "👍 这条我想做"},
                    "type": "primary",
                    "url": like_url,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "👎 没兴趣"},
                    "type": "default",
                    "url": dislike_url,
                },
            ],
        }

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

        # dbs 商业体检：只展示启动指数，后台细节不暴露
        health = td.get("commercial_health") or {}
        if health:
            idx = health.get("startup_index", 0) or 0
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"🩺 适合你启动 **{idx}/10**{_startup_suffix(idx)}"},
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

        if self.dry_run:
            # 演练：把卡片落到本地文件供检查，不发网络请求
            try:
                out_dir = Path(__file__).resolve().parent.parent / "storage" / "dry_run"
                out_dir.mkdir(parents=True, exist_ok=True)
                title = ""
                try:
                    title = card["header"]["title"]["content"]
                except (KeyError, TypeError):
                    pass
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                out = out_dir / f"card_{stamp}.json"
                out.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                logger.info(
                    "【演练】未发送。卡片「%s」%d 字节 → %s", title, size, out
                )
            except Exception as e:
                logger.warning("【演练】卡片落盘失败（不影响演练）: %s", e)
            return True

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
