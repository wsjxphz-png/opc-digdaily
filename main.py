#!/usr/bin/env python3
"""
双模块一人公司情报系统

  模块1 操盘手拆解：抓取 → 发现新操盘手 → 按人归档 → 轮转合成商业拆解卡（每天 1-2 人）
  模块2 赚钱机会挖掘：抓取 → 四维度评估 → 硬过滤卖铲子 → 国内/国际按内容质量动态分配（不硬性规定各 5 条）

用法:
  python main.py          # 立即执行一次
  python main.py --daemon # 启动定时服务，每天 9:00 执行
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import yaml

from ai import AIProcessor
from push import FeishuPusher
from sources import (
    YouTubeSource, RSSSource, RSSHubSource, RedditSource,
    TwitterSource, ChineseSearchSource, WeixinSearchSource, XiaoyuzhouSource,
    WeixinWhitelistSource, WeixinTargetSource, BilibiliSource, SourceScout, ContentItem,
)
from sources.base import has_strong_keyword
from sources.enricher import ContentEnricher
from operators import OperatorRoster
from teardown import TeardownEngine
from discovery import DiscoveryEngine
from opportunity import OpportunityEngine
from library import OpportunityLibrary
from feedback import FeedbackCollector, PreferenceProfile
from seed_facts import apply_seeds
from overflow_pool import OverflowPool

# 项目根目录
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
HISTORY_PATH = ROOT / "storage" / "history.json"
OVERFLOW_PATH = ROOT / "storage" / "overflow_pool.json"
ROSTER_PATH = ROOT / "storage" / "operators.json"
SEEDS_PATH = ROOT / "storage" / "seeded_facts.json"
LIBRARY_PATH = ROOT / "storage" / "opportunity_library.json"

# 北京时间
CST = timezone(timedelta(hours=8))

# 浏览器 UA（侦察兵复用搜狗微信账号搜索时需要，跟 weixin_search 一致）
_HEAD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weixin.sogou.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _RedactSecrets(logging.Filter):
    """飞书 webhook 是保密凭证，但 httpx 会把完整请求 URL 打进日志。

    只要日志里出现 open.feishu.cn 的 hook 路径，一律打码后再输出，
    避免日志文件/CI 输出被分享时泄露群机器人地址。
    """

    _HOOK = re.compile(r"(hook/)[A-Za-z0-9\-_]{6,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "hook/" in msg:
            record.msg = self._HOOK.sub(r"\1***REDACTED***", msg)
            record.args = ()
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactSecrets())

logger = logging.getLogger("main")


# ============================================================
# .env 加载
# ============================================================

def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value


# ============================================================
# 配置加载
# ============================================================

def load_config() -> dict:
    _load_dotenv()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if os.environ.get("FEISHU_WEBHOOK_URL"):
        config["feishu"]["webhook_url"] = os.environ["FEISHU_WEBHOOK_URL"]
    if os.environ.get("AI_API_KEY"):
        config["ai"]["api_key"] = os.environ["AI_API_KEY"]
    if os.environ.get("AI_API_BASE"):
        config["ai"]["api_base"] = os.environ["AI_API_BASE"]
    if os.environ.get("AI_MODEL"):
        config["ai"]["model"] = os.environ["AI_MODEL"]
    if os.environ.get("WEIXIN_RSS_BASE_URL"):
        config["weixin_whitelist"]["rss_base_url"] = os.environ["WEIXIN_RSS_BASE_URL"]

    return config


# ============================================================
# 去重历史
# ============================================================

class HistoryManager:
    def __init__(self, path: Path, days: int = 7):
        self.path = Path(path)
        self.days = days
        self._data: dict[str, str] = {}

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        self._prune()

    def _prune(self):
        cutoff = (datetime.now(CST) - timedelta(days=self.days)).strftime("%Y-%m-%d")
        self._data = {k: v for k, v in self._data.items() if v >= cutoff}

    def is_seen(self, url: str) -> bool:
        return url in self._data

    def mark_seen(self, url: str):
        self._data[url] = datetime.now(CST).strftime("%Y-%m-%d")

    def save(self):
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)


# ============================================================
# 管道：采集 + 过滤 + 去重 + 全文提取（不含 AI 判定/推送）
# ============================================================

def _build_sources(pipeline_cfg: dict) -> list:
    sources_cfg = pipeline_cfg.get("sources", {})
    sources = []
    if sources_cfg.get("rss", {}).get("enabled"):
        sources.append((RSSSource(), sources_cfg["rss"]))
    if sources_cfg.get("rsshub", {}).get("enabled"):
        sources.append((RSSHubSource(), sources_cfg["rsshub"]))
    if sources_cfg.get("reddit", {}).get("enabled"):
        sources.append((RedditSource(), sources_cfg["reddit"]))
    if sources_cfg.get("youtube", {}).get("enabled"):
        sources.append((YouTubeSource(), sources_cfg["youtube"]))
    if sources_cfg.get("twitter", {}).get("enabled"):
        sources.append((TwitterSource(), sources_cfg["twitter"]))
    if sources_cfg.get("chinese_search", {}).get("enabled"):
        sources.append((ChineseSearchSource(), sources_cfg["chinese_search"]))
    if sources_cfg.get("weixin_search", {}).get("enabled"):
        sources.append((WeixinSearchSource(), sources_cfg["weixin_search"]))
    if sources_cfg.get("weixin_whitelist", {}).get("enabled"):
        sources.append((WeixinWhitelistSource(), sources_cfg["weixin_whitelist"]))
    if sources_cfg.get("weixin_targets", {}).get("enabled"):
        sources.append((WeixinTargetSource(), sources_cfg["weixin_targets"]))
    if sources_cfg.get("xiaoyuzhou", {}).get("enabled"):
        sources.append((XiaoyuzhouSource(), sources_cfg["xiaoyuzhou"]))
    if sources_cfg.get("bilibili", {}).get("enabled"):
        sources.append((BilibiliSource(), sources_cfg["bilibili"]))
    return sources


async def _collect(
    label: str,
    pipeline_cfg: dict,
    history: HistoryManager,
) -> list[ContentItem]:
    """运行采集+过滤+去重+全文提取，返回候选内容（已含全文）。"""
    logger.info(f"===== [{label}] 采集 =====")
    start = time.time()

    sources = _build_sources(pipeline_cfg)
    keywords = pipeline_cfg.get("keywords", {})

    # Phase 1: 采集
    all_items: list[ContentItem] = []
    for source, source_cfg in sources:
        logger.info(f"  [{label}] [采集] {source.name}...")
        try:
            items = await source.fetch(source_cfg, keywords)
            all_items.extend(items)
        except Exception as e:
            logger.error(f"  [{label}] [失败] {source.name}: {e}")
    logger.info(f"[{label}] 采集完成: {len(all_items)} 条")

    # Phase 2: 强关键词预筛
    before = len(all_items)
    all_items = [
        it for it in all_items
        # 小宇宙/B站/公众号目标号 都是「按主题订阅 / 按名单定向」源，天然对题，
        # 豁免二次关键词预筛，直接进入 AI 评估（发现新 IP / 机会）
        if it.source == "xiaoyuzhou"
        or it.source == "bilibili"
        or it.source == "weixin_whitelist"
        or it.source == "weixin_targets"
        or has_strong_keyword(f"{it.title} {it.summary}", keywords)
    ]
    logger.info(f"[{label}] 预筛: {before} → {len(all_items)} 条")

    # Phase 3: 去重
    history.load()
    filtered = [it for it in all_items if not history.is_seen(it.url)]
    logger.info(f"[{label}] 去重后: {len(filtered)} 条")

    # Phase 4: 全文提取
    if filtered:
        logger.info(f"[{label}] 全文提取...")
        enricher = ContentEnricher()
        filtered = await enricher.enrich(filtered)

    elapsed = time.time() - start
    logger.info(f"===== [{label}] 采集完成 {elapsed:.1f}s =====")
    return filtered


# ============================================================
# 主控
# ============================================================

class DailyOpportunityBot:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = bool(dry_run)

        ai_cfg = config.get("ai", {})
        self.ai = AIProcessor(
            api_base=ai_cfg.get("api_base", ""),
            api_key=ai_cfg.get("api_key", ""),
            model=ai_cfg.get("model", "gpt-4o-mini"),
            max_tokens=ai_cfg.get("max_tokens", 8000),
            temperature=ai_cfg.get("temperature", 0.3),
        )

        fs_cfg = config.get("feishu", {})
        push_cfg = config.get("push", {})
        fb_cfg = config.get("feedback", {})
        self.feedback_enabled = bool(fb_cfg.get("enabled", False))
        self.feedback_repo = fb_cfg.get("github_repo", "") or ""
        self.feedback_boost = int(fb_cfg.get("boost_liked", 1))
        self.feedback_penalty = int(fb_cfg.get("penalize_disliked", 2))
        self.pusher = FeishuPusher(
            webhook_url=fs_cfg.get("webhook_url", ""),
            card_color=fs_cfg.get("card_color", "blue"),
            batch_size=push_cfg.get("batch_size", 8),
            feedback_repo=self.feedback_repo,
            feedback_enabled=self.feedback_enabled,
            dry_run=self.dry_run,
            ai_api_base=ai_cfg.get("api_base", ""),
            ai_api_key=ai_cfg.get("api_key", ""),
            ai_model=ai_cfg.get("model", ""),
        )

        self.history = HistoryManager(HISTORY_PATH)

        # 拆解 / 发现 配置
        td_cfg = config.get("teardown", {})
        self.teardown_enabled = td_cfg.get("enabled", True)
        self.teardown_per_day = td_cfg.get("per_day", 1)
        self.teardown_require_signals = td_cfg.get("require_signals", False)
        # 只推送「完全不会写代码的人」能做的方向：无 / 低
        self.allowed_tech_barrier = td_cfg.get("allowed_tech_barrier", ["无", "低"])
        # 聚焦「机会」：排除已成名多年的大V，只推新的一人公司 / 新 IP / 刚跑通一人模式的人
        self.exclude_established = td_cfg.get("exclude_established", True)
        # 复盘更新：对过去已拆解的操盘手补充新动态（新业务/新边界/新赚钱方式）
        self.revisit_per_day = td_cfg.get("revisit_per_day", 1)
        self.revisit_interval_days = td_cfg.get("revisit_interval_days", 14)

        disc_cfg = config.get("discovery", {})
        self.discovery_enabled = disc_cfg.get("enabled", True)
        self.discovery_max_scan = disc_cfg.get("max_scan", 30)

        # 模块2：赚钱机会挖掘 配置
        opp_cfg = config.get("opportunity", {})
        self.opportunity_enabled = opp_cfg.get("enabled", True)
        self.opportunity_per_region = opp_cfg.get("per_region", 20)
        self.exclude_shovel = opp_cfg.get("exclude_shovel", True)
        self.min_startup_index = int(opp_cfg.get("min_startup_index", 3))

        # 质量溢池：每日推送上限 + 高质量标杆 + 保质期
        self.daily_push_cap = int(opp_cfg.get("per_day", 10))
        self.quality_threshold = int(opp_cfg.get("quality_threshold", 7))

        # 跨天机会库
        lib_cfg = config.get("library", {})
        self.library_enabled = bool(lib_cfg.get("enabled", True))
        self.recurring_days = int(lib_cfg.get("recurring_days", 7))
        self.recurring_top = int(lib_cfg.get("recurring_top", 3))
        self.recurring_min_times = int(lib_cfg.get("recurring_min_times", 2))
        self.library = OpportunityLibrary(LIBRARY_PATH)

        # 引擎
        self.teardown_engine = TeardownEngine(self.ai)
        self.discovery_engine = DiscoveryEngine(self.ai, max_scan=self.discovery_max_scan)
        self.opportunity_engine = OpportunityEngine(
            self.ai, min_startup_index=self.min_startup_index
        )

    async def run(self):
        logger.info("========== 操盘手拆解系统启动 ==========")
        start = time.time()

        # ── 构建 / 加载操盘手名单 ──
        self.roster = OperatorRoster.build_from_config(self.config, ROSTER_PATH)
        # 套用种子事实 + 技术门槛分类（每次运行都重新应用，确保 indie hacker 被过滤）
        apply_seeds(self.roster, SEEDS_PATH)
        logger.info(f"当前名单: {self.roster.stats()}")

        # 内容源侦察兵改在 _collect 之后运行（见下方），以便从已采集的微信内容里
        # 稳定地挖出账号名，不再依赖被墙的搜狗账号搜索。

        # ── 采集两条管道 ──
        domestic_cfg = self.config.get("domestic", {})
        international_cfg = self.config.get("international", {})
        domestic_items = await _collect("国内", domestic_cfg, self.history)
        international_items = await _collect("国际", international_cfg, self.history)
        all_items = domestic_items + international_items

        # ── 内容源侦察兵：挖「写一人公司/本身是OPC」的公众号，自动入白名单 ──
        #   放在 _collect 之后：从当天已采集到的微信文章里直接取账号名（稳定），
        #   不再依赖被墙的搜狗账号搜索；DDG 主动发现作最佳努力兜底。
        #   通过的源写入 storage/scouted_sources.json，白名单次日即轮询（含 is_technical 过滤=只挖适合你的）。
        discovered_sources = []
        scout_cfg = self.config.get("source_scout", {})
        if scout_cfg.get("enabled", True) and self.ai.enabled:
            try:
                scout = SourceScout(ROOT / "storage" / "scouted_sources.json", scout_cfg)
                async with httpx.AsyncClient(
                    timeout=20, follow_redirects=True, headers=_HEAD
                ) as client:
                    discovered_sources = await scout.scout(
                        self.ai, client, domestic_items
                    )
                # 注入到白名单 accounts（次日即轮询；注册表也会持久化）
                wl_cfg = self.config["domestic"]["sources"]["weixin_whitelist"]
                existing = set(wl_cfg.get("accounts", []))
                for s in discovered_sources:
                    if s.name not in existing:
                        wl_cfg.setdefault("accounts", []).append(s.name)
                        existing.add(s.name)
                if discovered_sources:
                    logger.info(
                        "侦察兵新增 %d 个内容源并已注入白名单：%s",
                        len(discovered_sources),
                        "、".join(s.name for s in discovered_sources),
                    )
            except Exception as e:
                logger.exception(f"内容源侦察兵异常（已跳过，不影响主流程）: {e}")

        if not all_items:
            logger.warning("两条管道均无新内容，跳过")
            elapsed = time.time() - start
            logger.info(f"========== 完成 (无内容) {elapsed:.1f}s ==========")
            return

        # ============================================================
        # 模块1：操盘手拆解 + 发现
        # ============================================================
        # ── 发现循环：从内容里找新操盘手 ──
        discovered = []
        if self.discovery_enabled and self.ai.enabled:
            scanned = await self.discovery_engine.scan(all_items, self.roster)
            # 只保留「非技术(无/低)且成功入名单」的发现推送给用户；
            # commit 内部会排除中/高(需写代码)以及已存在的人
            for d in scanned:
                if await d.commit(self.roster):
                    discovered.append(d)
            if discovered:
                logger.info(f"发现 {len(discovered)} 名新操盘手（非技术），已加入名单")

        # ── 按人归档信号 ──
        acc = 0
        for it in all_items:
            if self.roster.accumulate(it):
                acc += 1
        logger.info(f"信号归档: {acc} 条内容归入操盘手档案")
        self._save_roster()

        # ── 拆解循环：轮转合成拆解卡 ──
        teardowns = []
        if self.teardown_enabled and self.ai.enabled:
            # 先拉一个大候选池，再按技术门槛过滤（只保留 无/低），取前 per_day
            due_pool = self.roster.get_due_for_teardown(
                200,
                require_signals=self.teardown_require_signals,
                allowed_tech_barrier=self.allowed_tech_barrier,
                exclude_established=self.exclude_established,
            )
            due = due_pool[: self.teardown_per_day]
            logger.info(
                f"今日待拆解(非技术 {self.allowed_tech_barrier}): "
                f"{len(due)} 人（取前 {self.teardown_per_day}）"
            )
            for op in due:
                td = await self.teardown_engine.synthesize(op)
                # dbs 商业底层逻辑体检判为「结构性坏案例」的，直接不推（保护初学者）
                if td and op.commercial_severity != "skip":
                    teardowns.append(td.to_dict())
                elif op.commercial_severity == "skip":
                    logger.info(f"[{op.name}] dbs 商业体检未过，跳过推送")
            self._save_roster()

        # ── 复盘更新循环：对过去已拆解的操盘手补充新动态（新业务/新边界/新赚钱方式）──
        if self.teardown_enabled and self.ai.enabled:
            revisit_pool = self.roster.get_due_for_revisit(
                self.revisit_per_day,
                interval_days=self.revisit_interval_days,
                allowed_tech_barrier=self.allowed_tech_barrier,
            )
            for op in revisit_pool:
                td = await self.teardown_engine.synthesize_revisit(op, op.teardown)
                # 复盘重算后若判为「结构性坏案例」，同样不推
                if td and op.commercial_severity != "skip":
                    teardowns.append(td.to_dict())
                elif op.commercial_severity == "skip":
                    logger.info(f"[{op.name}] 复盘后 dbs 商业体检未过，跳过推送")
            if revisit_pool:
                logger.info(f"今日复盘更新: {len(revisit_pool)} 人")
            self._save_roster()

        # ============================================================
        # 模块2：赚钱机会挖掘（国内 / 国际，剔除卖铲子）
        # ============================================================
        dom_opps, intl_opps = [], []
        if self.opportunity_enabled and self.ai.enabled:
            dom_opps, intl_opps = await self.opportunity_engine.mine(
                domestic_items, international_items, self.opportunity_per_region
            )

        # ============================================================
        # 跨天机会库 + 反馈闭环
        #   1) 读回昨天之前用户点的 👍/👎（GitHub Issues）→ 写进机会库
        #   2) 把今天的机会并入机会库，标注「第N次出现 / M个来源印证」
        #   3) 按历史口味微调排序（±1~2 分，刻意克制，避免一两次点击带偏系统）
        # ============================================================
        recurring = []
        if self.library_enabled and (dom_opps or intl_opps):
            try:
                self.library.load()

                if self.feedback_enabled and self.feedback_repo:
                    collector = FeedbackCollector(
                        self.feedback_repo,
                        token=os.environ.get("GITHUB_TOKEN", ""),
                        enabled=True,
                    )
                    tallies = await collector.fetch()
                    if tallies:
                        n = self.library.apply_feedback(tallies)
                        logger.info(f"反馈已并入机会库：{n} 个主题带有 👍/👎")

                today = datetime.now(CST).strftime("%Y-%m-%d")
                self.library.annotate(dom_opps, today)
                self.library.annotate(intl_opps, today)

                if self.feedback_enabled:
                    profile = PreferenceProfile.from_library(
                        self.library, self.feedback_boost, self.feedback_penalty
                    )
                    if profile.active:
                        profile.adjust(dom_opps)
                        profile.adjust(intl_opps)
                        logger.info(
                            "已按历史反馈调整排序（喜欢 %d 类 / 不喜欢 %d 类）",
                            len(profile.liked), len(profile.disliked),
                        )

                recurring = self.library.top_recurring(
                    days=self.recurring_days,
                    limit=self.recurring_top,
                    min_times=self.recurring_min_times,
                )
                if self.dry_run:
                    logger.info("【演练】机会库不落盘，避免污染真实的出现次数统计")
                else:
                    self.library.save()
                logger.info(self.library.stats())
            except Exception as e:
                logger.exception(f"机会库/反馈处理异常（已跳过，不影响推送）: {e}")

        # ── 质量溢池：合并昨日滞留，限制每日推送 ≤ 10 条 ──
        total_before_pool = len(dom_opps) + len(intl_opps)
        if dom_opps or intl_opps:
            all_opps = dom_opps + intl_opps
            pool = OverflowPool(
                OVERFLOW_PATH,
                daily_cap=self.daily_push_cap,
                quality_threshold=self.quality_threshold,
                max_age_days=int(opp_cfg.get("overflow_days", 3)),
            )
            today_str = datetime.now(CST).strftime("%Y-%m-%d")
            pushed, overflowed = pool.decide(all_opps, today_str)
            # 重新按 region 分拆
            dom_opps = [it for it in pushed if it in dom_opps or getattr(it, "source_name", "") in [d.source_name for d in domestic_items[:1]]]
            # 简化：track region via original list membership
            dom_urls = set(getattr(it, "url", "") for it in domestic_items)
            dom_opps = [it for it in pushed if getattr(it, "url", "") in dom_urls]
            intl_opps = [it for it in pushed if getattr(it, "url", "") not in dom_urls]
            if overflowed:
                logger.info(
                    f"溢池：{total_before_pool} → 今日推送 {len(pushed)} / 入池 {len(overflowed)}（标杆≥{self.quality_threshold}）"
                )

        # ── 推送 / 输出 ──
        date_str = datetime.now(CST).strftime("%Y年%m月%d日")
        pushed_any = False

        # 模块1：操盘手拆解（OPC赚钱机会挖掘日报 · 操盘手拆解）
        if teardowns or discovered:
            if self.pusher.enabled:
                disc_dicts = [dataclasses.asdict(d) for d in discovered]
                ok = await self.pusher.push_teardowns(teardowns, disc_dicts, date_str)
                if ok:
                    pushed_any = True
                    logger.info(
                        f"模块1 推送成功: 拆解 {len(teardowns)} 人 + 新发现 {len(discovered)} 人"
                    )
                else:
                    logger.error("模块1 推送失败")
            else:
                self._cli_output_teardowns(teardowns, discovered)

        # 模块2：赚钱机会挖掘（OPC赚钱机会挖掘日报）
        if dom_opps or intl_opps:
            if self.pusher.enabled:
                ok = await self.pusher.push_opportunities(
                    dom_opps, intl_opps, date_str, recurring,
                    screened_out=total_before_pool - len(pushed) if 'pushed' in dir() else 0,
                    screened_total=total_before_pool,
                )
                if ok:
                    pushed_any = True
                    logger.info(
                        f"模块2 推送成功: 国内 {len(dom_opps)} + 国际 {len(intl_opps)}"
                    )
                else:
                    logger.error("模块2 推送失败")
            else:
                self._cli_output_opportunities(dom_opps, intl_opps, recurring)

        # 内容源侦察兵：今日新挖掘并加入白名单的内容源（公众号）
        if discovered_sources:
            if self.pusher.enabled:
                ok = await self.pusher.push_scouted_sources(discovered_sources, date_str)
                if ok:
                    pushed_any = True
                    logger.info(f"侦察兵推送成功: 新内容源 {len(discovered_sources)} 个")
                else:
                    logger.error("侦察兵推送失败")
            else:
                print(f"\n========== [侦察兵] 新挖掘内容源 {len(discovered_sources)} 个 ==========")
                for s in discovered_sources:
                    print(f"  📡 {s.name}（质量 {s.score}/10）— {s.reason}")

        if not (teardowns or discovered or dom_opps or intl_opps or discovered_sources):
            logger.warning("今日两模块均无内容可推送")

        # 去重标记：任一模块成功推送，即把今日采到的内容标记为已读
        # 演练时绝不能标记，否则真实运行会以为这些内容"昨天推过了"而漏掉
        if pushed_any and not self.dry_run:
            for it in all_items:
                self.history.mark_seen(it.url)
            self.history.save()
        elif pushed_any and self.dry_run:
            logger.info("【演练】不写去重历史，%d 条内容仍可在正式运行时推送", len(all_items))

        elapsed = time.time() - start
        logger.info(f"========== 全部完成 {elapsed:.1f}s ==========")

    def _save_roster(self):
        """保存操盘手名单。演练模式下不落盘——名单里记着"谁已经拆解过、下次轮到谁"，
        演练写进去会让正式运行跳过这些人。"""
        if self.dry_run:
            logger.debug("【演练】操盘手名单不落盘")
            return
        self.roster.save()

    def _cli_output_teardowns(self, teardowns: list[dict], discovered: list):
        if teardowns:
            print("\n========== [模块1] 今日拆解卡 ==========")
            for td in teardowns:
                print(f"\n🔍 {td.get('operator_name')} ({td.get('region')})")
                for k in ("who", "deliverable", "business_model", "acquisition",
                          "stack", "first_step", "red_flag", "learn"):
                    if td.get(k):
                        print(f"  {k}: {td[k]}")
                print(f"  可复制性: {td.get('replicability')}/5")
        if discovered:
            print("\n========== [模块1] 新发现操盘手 ==========")
            for d in discovered:
                print(f"  🆕 {d.name} ({d.region}) — {d.highlight}")

    def _cli_output_opportunities(self, dom_opps: list, intl_opps: list, recurring: list = None):
        print("\n========== [模块2] 赚钱机会 ==========")
        print(f"🇨🇳 国内 {len(dom_opps)} 条 / 🌍 国际 {len(intl_opps)} 条")
        if recurring:
            print("🔁 本周反复出现的方向：")
            for r in recurring:
                print(f"   · {r['topic']} — 第{r['times']}次，{r['sources']}个来源印证")
        for it in dom_opps + intl_opps:
            print(
                f"  💡 {it.title[:60]}\n"
                f"     适合你启动 {getattr(it, 'startup_index', 0)}/10 · "
                f"商业化 {getattr(it, 'commercial_score', 0)}/100 · "
                f"可行性 {getattr(it, 'feasibility_score', 0)}/100"
            )
            if getattr(it, "score_reason", ""):
                print(f"     └ {it.score_reason}")
            tpl = getattr(it, "copy_template", None) or {}
            if tpl.get("first_step"):
                print(f"     📋 第一步：{tpl['first_step']}")


async def run_once(dry_run: bool = False):
    config = load_config()
    bot = DailyOpportunityBot(config, dry_run=dry_run)
    await bot.run()


def run_daemon():
    import schedule
    config = load_config()
    sched = config.get("schedule", {})
    hour = sched.get("hour", 9)
    minute = sched.get("minute", 0)

    logger.info(f"定时服务已启动，每天 {hour:02d}:{minute:02d} (北京时间) 执行")

    async def job():
        try:
            await run_once()
        except Exception as e:
            logger.exception(f"执行失败: {e}")

    def _runner():
        asyncio.run(job())

    logger.info("首次执行...")
    _runner()

    schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(_runner)

    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="操盘手拆解系统 每日推送")
    parser.add_argument("--daemon", action="store_true", help="启动定时服务模式")
    parser.add_argument("--once", action="store_true", help="立即执行一次 (默认)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练：完整跑一遍并把卡片存到 storage/dry_run/，但不发到群、不写任何历史记录",
    )
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        if args.dry_run:
            logger.info("========== 演练模式：不推送、不落盘 ==========")
        asyncio.run(run_once(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
