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
    YouTubeSource, RSSSource, RedditSource, TwitterSource, ContentItem,
)
from sources.base import has_strong_keyword

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
# 配置加载
# ============================================================

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 环境变量覆盖敏感字段 (适合 GitHub Actions / CI)
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
    """简单文件去重，记录已推送的 URL。"""

    def __init__(self, path: Path, days: int = 7):
        self.path = Path(path)
        self.days = days
        self._data: dict[str, str] = {}  # url -> date_str

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        self._prune()

    def _prune(self):
        """清理过期记录。"""
        cutoff = (datetime.now(CST) - timedelta(days=self.days)).strftime("%Y-%m-%d")
        self._data = {
            k: v for k, v in self._data.items() if v >= cutoff
        }

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
# 主流程
# ============================================================

class DailyOpportunityBot:
    """主控类。"""

    def __init__(self, config: dict):
        self.config = config
        self.keywords = config.get("keywords", {})
        self.push_max = config.get("schedule", {}).get("push_max", 10)

        # 初始化各模块
        ai_cfg = config.get("ai", {})
        self.ai = AIProcessor(
            api_base=ai_cfg.get("api_base", ""),
            api_key=ai_cfg.get("api_key", ""),
            model=ai_cfg.get("model", "gpt-4o-mini"),
            max_tokens=ai_cfg.get("max_tokens", 800),
            temperature=ai_cfg.get("temperature", 0.3),
        )

        fs_cfg = config.get("feishu", {})
        self.pusher = FeishuPusher(
            webhook_url=fs_cfg.get("webhook_url", ""),
            card_color=fs_cfg.get("card_color", "blue"),
        )

        self.history = HistoryManager(HISTORY_PATH)

        # 信息源 registry
        self.sources = [
            (YouTubeSource(), config.get("sources", {}).get("youtube", {})),
            (RSSSource(), config.get("sources", {}).get("rss", {})),
            (RedditSource(), config.get("sources", {}).get("reddit", {})),
            (TwitterSource(), config.get("sources", {}).get("twitter", {})),
        ]

    async def run(self):
        """执行一次完整流程。"""
        logger.info("===== 开始每日信息采集 =====")
        start = time.time()

        # Phase 1: 并行采集所有源 (采集时用弱关键词初筛)
        logger.info("[1/5] 采集信息源...")
        all_items: list[ContentItem] = []
        for source, source_cfg in self.sources:
            if not source_cfg.get("enabled", False):
                logger.info(f"  [跳过] {source.name}")
                continue
            logger.info(f"  [采集] {source.name}...")
            try:
                items = await source.fetch(source_cfg, self.keywords)
                all_items.extend(items)
            except Exception as e:
                logger.error(f"  [失败] {source.name}: {e}")

        logger.info(f"采集完成: 共 {len(all_items)} 条原始内容")

        # Phase 2: 强关键词预筛 — 必须命中至少 1 个 OPC 专属词
        logger.info("[2/5] 强关键词预筛选...")
        before = len(all_items)
        all_items = [
            it for it in all_items
            if has_strong_keyword(f"{it.title} {it.summary}", self.keywords)
        ]
        logger.info(f"预筛后: {before} → {len(all_items)} 条 (仅保留 OPC 强相关内容)")

        # Phase 3: 去重
        logger.info("[3/5] 去重...")
        self.history.load()
        filtered = [it for it in all_items if not self.history.is_seen(it.url)]
        logger.info(f"去重后: {len(filtered)} 条新内容")

        if not filtered:
            logger.warning("无新内容，跳过推送")
            elapsed = time.time() - start
            logger.info(f"===== 完成 (无推送)，耗时 {elapsed:.1f}s =====")
            return

        # Phase 4: AI 批量处理 (一次 API 调用)
        logger.info("[4/5] AI 批量翻译+总结+机会挖掘...")
        top_items = []
        if self.ai.enabled:
            filtered = await self.ai.process(filtered)
            # 只保留 AI 判定为相关且得分 >= 门槛的
            from ai.processor import MIN_SCORE
            top_items = [
                it for it in filtered
                if it.ai_processed and it.relevance_score >= MIN_SCORE
            ]
            # 按得分排序
            top_items.sort(key=lambda x: x.relevance_score, reverse=True)
            # 截取 Top N
            top_items = top_items[:self.push_max]
            logger.info(f"AI 筛选后: {len(filtered)} → {len(top_items)} 条推送候选")
            if top_items:
                logger.info("Top 推送:")
                for it in top_items:
                    logger.info(
                        f"  [{it.relevance_score:.2f}] {it.title[:60]}... "
                        f"| {it.opportunity_hint[:40]}"
                    )
        else:
            logger.warning("AI 未配置，无法进行内容筛选，跳过推送")
            elapsed = time.time() - start
            logger.info(f"===== 完成 (AI未配置)，耗时 {elapsed:.1f}s =====")
            return

        if not top_items:
            logger.warning("AI 筛选后无达标内容，跳过推送")
            elapsed = time.time() - start
            logger.info(f"===== 完成 (无达标内容)，耗时 {elapsed:.1f}s =====")
            return

        # Phase 5: 飞书推送
        logger.info("[5/5] 推送飞书...")
        date_str = datetime.now(CST).strftime("%Y年%m月%d日")
        if self.pusher.enabled:
            ok = await self.pusher.push_daily_report(top_items, date_str)
            if ok:
                for it in top_items:
                    self.history.mark_seen(it.url)
                self.history.save()
                logger.info(f"推送成功: {len(top_items)} 条")
            else:
                logger.error("推送失败")
        else:
            logger.warning("飞书未配置，跳过推送")
            for it in top_items:
                print(f"\n--- [{it.source}] {it.title[:80]} — {it.relevance_score:.2f}")
                print(f"    URL: {it.url}")
                if it.ai_summary:
                    print(f"    总结: {it.ai_summary}")
                if it.opportunity_hint:
                    print(f"    机会: {it.opportunity_hint}")

        elapsed = time.time() - start
        logger.info(f"===== 完成，耗时 {elapsed:.1f}s =====")


async def run_once():
    config = load_config()
    bot = DailyOpportunityBot(config)
    await bot.run()


def run_daemon():
    """定时服务模式。"""
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

    # 先立即执行一次
    logger.info("首次执行...")
    _runner()

    schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(_runner)

    while True:
        schedule.run_pending()
        time.sleep(60)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="一人公司赚钱机会挖掘 每日推送系统")
    parser.add_argument(
        "--daemon", action="store_true",
        help="启动定时服务模式"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="立即执行一次 (默认)"
    )
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        asyncio.run(run_once())


if __name__ == "__main__":
    main()
