"""
质量溢池：当今天的好机会超过每日推送上限（10条）时，
把多出来的高质量内容暂存起来，明天优先推送，避免浪费。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sources.base import ContentItem
from opportunity import effective_index

logger = logging.getLogger(__name__)


class OverflowPool:
    """跨日质量溢池——按启动指数排序，保质期 3 天。"""

    def __init__(self, path: Path, daily_cap: int = 10, quality_threshold: int = 7,
                 max_age_days: int = 3):
        self._path = path
        self._daily_cap = daily_cap
        self._quality_threshold = quality_threshold
        self._max_age_days = max_age_days
        self._items: list[dict] = []
        self._load()

    @property
    def daily_cap(self) -> int:
        return self._daily_cap

    def _load(self):
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = raw.get("items", [])
        except Exception:
            self._items = []

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "items": self._items}
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ----------------------------------------------------------------
    # 公开接口
    # ----------------------------------------------------------------
    def decide(
        self, today_items: list[ContentItem], date_str: str
    ) -> tuple[list[ContentItem], list[ContentItem]]:
        """合并溢池 + 今日 → 排序 → 分出「今日推送」与「入池留到明天」。

        Returns:
            (pushed,  pool_candidates)
                pushed: 今天应该推送的（≤ daily_cap）
                pool_candidates: 放入溢池、明天再用的
        """
        all_items = list(today_items)  # 不修改原列表

        # 1. 从溢池恢复昨天的滞留内容
        restored = self._restore(date_str)
        if restored:
            logger.info(f"溢池恢复：{len(restored)} 条昨日滞留 → 加入今日候选")

        all_items.extend(restored)

        # 2. 去重：同 URL 只保留最新的
        seen = {}
        for it in all_items:
            url = getattr(it, "url", "")
            si = getattr(it, "startup_index", 0) or 0
            if url:
                if url in seen:
                    # 保留分数更高的那条
                    existing_si = getattr(seen[url], "startup_index", 0) or 0
                    if si > existing_si:
                        seen[url] = it
                else:
                    seen[url] = it
            else:
                seen[f"__nourl_{id(it)}"] = it

        all_items = list(seen.values())

        # 3. 按「降权后」启动指数降序排列（红海机会软降权，但不改硬门槛）
        all_items.sort(
            key=lambda it: (effective_index(it),),
            reverse=True,
        )

        # 4. 取出每日上限
        pushed = all_items[:self._daily_cap]
        pool_candidates = all_items[self._daily_cap:]

        # 5. 溢池只保留「高质量」的内容（低于阈值的不值得等到明天）
        pool_candidates = [
            it for it in pool_candidates
            if (getattr(it, "startup_index", 0) or 0) >= self._quality_threshold - 1
        ]

        # 6. 存入溢池文件
        self._items = self._serialize(pool_candidates, date_str)
        self.save()

        if pool_candidates:
            logger.info(
                f"溢池入池：{len(pool_candidates)} 条（启动指数 ≥ {self._quality_threshold - 1}），明天优先推送"
            )

        return pushed, pool_candidates

    def stats_for_card(self) -> dict:
        """返回卡片顶部信任摘要所需的数据。"""
        return {
            "pool_size": len(self._items),
            "quality_threshold": self._quality_threshold,
            "daily_cap": self._daily_cap,
        }

    # ----------------------------------------------------------------
    # 内部
    # ----------------------------------------------------------------
    def _restore(self, today: str) -> list[ContentItem]:
        """从溢池 JSON 反序列化为 ContentItem，剔除过期条目。"""
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        cutoff = today_dt - timedelta(days=self._max_age_days)
        restored = []
        kept = []
        for entry in self._items:
            item_date = entry.get("pooled_on", "")
            try:
                dt = datetime.strptime(item_date, "%Y-%m-%d")
            except ValueError:
                dt = today_dt
            if dt < cutoff:
                continue  # 过期丢弃
            restored.append(self._deserialize(entry))
            kept.append(entry)
        self._items = kept
        return restored

    def _serialize(self, items: list[ContentItem], date_str: str) -> list[dict]:
        return [
            {
                "title": getattr(it, "title", ""),
                "url": getattr(it, "url", ""),
                "translation": getattr(it, "translation", ""),
                "ai_summary": getattr(it, "ai_summary", ""),
                "startup_index": getattr(it, "startup_index", 0) or 0,
                "commercial_score": getattr(it, "commercial_score", 0) or 0,
                "feasibility_score": getattr(it, "feasibility_score", 0) or 0,
                "code_dependency": getattr(it, "code_dependency", 0) or 0,
                "authenticity": getattr(it, "authenticity", 0) or 0,
                "verdict": getattr(it, "verdict", ""),
                "difficulty": getattr(it, "difficulty", ""),
                "practical_steps": getattr(it, "practical_steps", ""),
                "opportunity_hint": getattr(it, "opportunity_hint", ""),
                "xhs_title": getattr(it, "xhs_title", ""),
                "source_name": getattr(it, "source_name", "") or getattr(it, "source", ""),
                "copy_template": getattr(it, "copy_template", None) or {},
                "score_reason": getattr(it, "score_reason", ""),
                "gate_reason": getattr(it, "gate_reason", ""),
                "repeat_count": getattr(it, "repeat_count", 0) or 0,
                "corroborations": getattr(it, "corroborations", 0) or 0,
                "first_seen": getattr(it, "first_seen", ""),
                "pooled_on": date_str,
            }
            for it in items
        ]

    def _deserialize(self, d: dict) -> ContentItem:
        it = ContentItem(
            title=d.get("title", ""),
            url=d.get("url", ""),
            source=d.get("source_name", ""),
        )
        it.translation = d.get("translation", "")
        it.ai_summary = d.get("ai_summary", "")
        it.startup_index = d.get("startup_index", 0)
        it.commercial_score = d.get("commercial_score", 0)
        it.feasibility_score = d.get("feasibility_score", 0)
        it.code_dependency = d.get("code_dependency", 0)
        it.authenticity = d.get("authenticity", 0)
        it.verdict = d.get("verdict", "")
        it.difficulty = d.get("difficulty", "")
        it.practical_steps = d.get("practical_steps", "")
        it.opportunity_hint = d.get("opportunity_hint", "")
        it.xhs_title = d.get("xhs_title", "")
        it.source_name = d.get("source_name", "")
        it.copy_template = d.get("copy_template", {})
        it.score_reason = d.get("score_reason", "")
        it.gate_reason = d.get("gate_reason", "")
        it.repeat_count = d.get("repeat_count", 0)
        it.corroborations = d.get("corroborations", 0)
        it.first_seen = d.get("first_seen", "")
        return it
