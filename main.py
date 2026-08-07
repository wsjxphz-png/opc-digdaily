#!/usr/bin/env python3
"""
一人公司赚钱机会挖掘 — 每日信息采集+AI处理+飞书推送系统

用法:
  python main.py          # 立即执行一次
  python main.py --daemon # 启动定时服务，每天 9:00 执行
  python main.py --once   # 立即执行一次 (同无参数)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from ai import AIProcessor
from push import FeishuPusher
from sources import (
    YouTubeSource, RSSSource, RSSHubSource, RedditSource,
    TwitterSource, ChineseSearchSource, ContentItem,
)
from sources.base import has_strong_keyword
from sources.enricher import ContentEnricher

# 项目根目录
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
HISTORY_PATH = ROOT / "storage" / "history.json"

# 北京时间
CST = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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
# 管道运行
# ============================================================

def _build_sources(pipeline_cfg: dict) -> list:
    """根据管道配置构建信息源列表。"""
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
    return sources


async def _run_pipeline(
    label: str,
    pipeline_cfg: dict,
    ai: AIProcessor,
    history: HistoryManager,
) -> list[ContentItem]:
    """运行一条完整管道，返回推送候选列表。"""
    logger.info(f"===== [{label}] 开始 =====")
    start = time.time()

    sources = _build_sources(pipeline_cfg)
    keywords = pipeline_cfg.get("keywords", {})
    push_max = pipeline_cfg.get("push_max", 5)

    # Phase 1: 采集
    logger.info(f"[{label}] 1/5 采集信息源...")
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
    logger.info(f"[{label}] 2/5 关键词预筛...")
    before = len(all_items)
    all_items = [
        it for it in all_items
        if has_strong_keyword(f"{it.title} {it.summary}", keywords)
    ]
    logger.info(f"[{label}] 预筛: {before} → {len(all_items)} 条")

    # Phase 3: 去重
    logger.info(f"[{label}] 3/5 去重...")
    history.load()
    filtered = [it for it in all_items if not history.is_seen(it.url)]
    logger.info(f"[{label}] 去重后: {len(filtered)} 条")

    if not filtered:
        logger.warning(f"[{label}] 无新内容，跳过")
        elapsed = time.time() - start
        logger.info(f"===== [{label}] 完成 (无推送) {elapsed:.1f}s =====")
        return []

    # Phase 4: 全文提取
    logger.info(f"[{label}] 4/5 全文提取...")
    enricher = ContentEnricher()
    filtered = await enricher.enrich(filtered)

    # Phase 5: AI 处理
    logger.info(f"[{label}] 5/5 AI 筛选+总结...")
    top_items = []
    if ai.enabled:
        filtered = await ai.process(filtered)
        from ai.processor import MIN_SCORE
        top_items = [
            it for it in filtered
            if it.ai_processed and it.relevance_score >= MIN_SCORE
        ]
        top_items.sort(key=lambda x: x.relevance_score, reverse=True)
        top_items = top_items[:push_max]
        logger.info(f"[{label}] AI 筛选: {len(filtered)} → {len(top_items)} 条")
        if top_items:
            for it in top_items:
                flag = getattr(it, "quality_flag", "") or ""
                flag_str = f" {flag}" if flag else ""
                logger.info(
                    f"  [{it.relevance_score:.2f}{flag_str}] {it.title[:60]}..."
                )
    else:
        logger.warning(f"[{label}] AI 未配置，跳过")

    elapsed = time.time() - start
    logger.info(f"===== [{label}] 完成 {elapsed:.1f}s =====")
    return top_items


# ============================================================
# 主控
# ============================================================

class DailyOpportunityBot:
    def __init__(self, config: dict):
        self.config = config

        ai_cfg = config.get("ai", {})
        self.ai = AIProcessor(
            api_base=ai_cfg.get("api_base", ""),
            api_key=ai_cfg.get("api_key", ""),
            model=ai_cfg.get("model", "gpt-4o-mini"),
            max_tokens=ai_cfg.get("max_tokens", 8000),
            temperature=ai_cfg.get("temperature", 0.3),
        )

        fs_cfg = config.get("feishu", {})
        self.pusher = FeishuPusher(
            webhook_url=fs_cfg.get("webhook_url", ""),
            card_color=fs_cfg.get("card_color", "blue"),
        )

        self.history = HistoryManager(HISTORY_PATH)

    async def run(self):
        logger.info("========== 开始每日双管道推送 ==========")
        start = time.time()

        # ---- 国内管道 ----
        domestic_cfg = self.config.get("domestic", {})
        domestic_items = await _run_pipeline("国内", domestic_cfg, self.ai, self.history)

        # ---- 国际管道 ----
        international_cfg = self.config.get("international", {})
        international_items = await _run_pipeline("国际", international_cfg, self.ai, self.history)

        # ---- 合并推送 ----
        if not domestic_items and not international_items:
            logger.warning("两条管道均无推送内容")
            elapsed = time.time() - start
            logger.info(f"========== 完成 (无推送) {elapsed:.1f}s ==========")
            return

        date_str = datetime.now(CST).strftime("%Y年%m月%d日")

        if self.pusher.enabled:
            ok = await self.pusher.push_dual_report(
                domestic_items, international_items, date_str
            )
            if ok:
                for it in domestic_items + international_items:
                    self.history.mark_seen(it.url)
                self.history.save()
                logger.info(
                    f"推送成功: 国内 {len(domestic_items)} 条 + 国际 {len(international_items)} 条"
                )
            else:
                logger.error("推送失败")
        else:
            logger.warning("飞书未配置")
            for label, items in [("国内", domestic_items), ("国际", international_items)]:
                if not items:
                    continue
                logger.info(f"\n--- {label} ---")
                for it in items:
                    print(f"\n  [{it.source_name}] {it.title[:80]} — {it.relevance_score:.2f}")
                    print(f"  URL: {it.url}")
                    if it.ai_summary:
                        print(f"  大意: {it.ai_summary}")
                    if it.opportunity_hint:
                        print(f"  模仿: {it.opportunity_hint}")

        elapsed = time.time() - start
        logger.info(f"========== 全部完成 {elapsed:.1f}s ==========")


async def run_once():
    config = load_config()
    bot = DailyOpportunityBot(config)
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
    parser = argparse.ArgumentParser(description="一人公司赚钱机会挖掘 每日推送系统")
    parser.add_argument("--daemon", action="store_true", help="启动定时服务模式")
    parser.add_argument("--once", action="store_true", help="立即执行一次 (默认)")
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        asyncio.run(run_once())


if __name__ == "__main__":
    main()
