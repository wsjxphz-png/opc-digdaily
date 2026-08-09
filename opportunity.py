"""
赚钱机会挖掘引擎 — 模块2：一人公司相关的商业/赚钱机会挖掘。

输入：国内 / 国际两条采集管道的内容（已含全文）
处理：复用 AIProcessor.process() 的「无代码商业分析师」四维度 + 三条否决评估
输出：经过硬过滤（剔除卖铲子）的机会清单，国内、国际各取 top N

硬过滤规则（双保险，绝不推送卖铲子信息）：
  - 必须经过 AI 评估（ai_processed=True）
  - 结论必须是「可复刻的真机会」（verdict 含"真机会"）
  - 真实性 >= 3（<=2 视为卖课/卖铲子）
  - 代码依赖 <= 3（>=4 需写代码，普通人做不了）
  - 综合相关度 >= 0.4（由 scoring.py 固定公式算出，非 AI 自评）
  - 适合你启动指数 >= min_startup_index（默认 4，可在 config 调）

排序口径：先看「适合你启动指数」（1-10），同分再看综合分——
用户是照着抄的角度，"能不能落地"永远优先于"生意天花板高不高"。
"""
import logging
from typing import Optional

from ai import AIProcessor
from sources.base import ContentItem
from filters import is_technical

logger = logging.getLogger(__name__)

# 适合你启动指数低于此值 → 不推（默认 4：低于 4 意味着落地路径不清或有致命短板）
DEFAULT_MIN_STARTUP_INDEX = 4


def _is_real_opportunity(it: ContentItem, min_startup_index: int = DEFAULT_MIN_STARTUP_INDEX) -> bool:
    """判断一条内容是否为「可复刻的真机会」，且不是卖铲子。"""
    if not getattr(it, "ai_processed", False):
        return False
    verdict = it.verdict or ""
    if "真机会" not in verdict:
        return False
    if (it.authenticity or 0) < 3:
        return False
    if (it.code_dependency or 0) >= 4:
        return False
    if (it.relevance_score or 0) < 0.4:
        return False
    idx = getattr(it, "startup_index", 0) or 0
    if idx and idx < min_startup_index:
        logger.info(
            "机会 [%s] 适合你启动指数仅 %d 分（<%d），已排除",
            (getattr(it, "title", "") or "")[:30], idx, min_startup_index,
        )
        return False
    # 双保险：读者完全不懂代码，含强技术信号的文章一律不推
    probe = f"{getattr(it, 'title', '')} {getattr(it, 'summary', '')}"
    if is_technical(probe):
        logger.info(f"机会 [{getattr(it, 'title', '')[:30]}] 命中技术关键词，已排除（不推技术向文章）")
        return False
    return True


class OpportunityEngine:
    def __init__(self, ai: AIProcessor, min_startup_index: int = DEFAULT_MIN_STARTUP_INDEX):
        self.ai = ai
        self.min_startup_index = int(min_startup_index or DEFAULT_MIN_STARTUP_INDEX)

    async def mine(
        self,
        domestic_items: list[ContentItem],
        international_items: list[ContentItem],
        per_region: int = 15,
    ) -> tuple[list[ContentItem], list[ContentItem]]:
        """挖掘国内 / 国际赚钱机会，返回通过硬过滤的全部优质内容。

        per_region 仅作为「安全上限」（防止极端情况下条数失控），不是目标条数——
        命中硬过滤的优质内容不封顶，有多少推多少。AI 未启用时返回 ([], [])。
        """
        if not self.ai.enabled:
            logger.warning("AI 未启用，跳过机会挖掘")
            return [], []

        domestic = (
            await self.ai.process(domestic_items) if domestic_items else []
        )
        international = (
            await self.ai.process(international_items) if international_items else []
        )

        dom_opps = self._select(domestic, per_region, self.min_startup_index)
        intl_opps = self._select(international, per_region, self.min_startup_index)

        logger.info(
            f"机会挖掘：国内 {len(dom_opps)} 条 / 国际 {len(intl_opps)} 条 "
            f"（候选 {len(domestic)} / {len(international)}，安全上限 {per_region}）"
        )
        return dom_opps, intl_opps

    @staticmethod
    def _select(
        items: list[ContentItem],
        per_region: int,
        min_startup_index: int = DEFAULT_MIN_STARTUP_INDEX,
    ) -> list[ContentItem]:
        """返回通过硬过滤的全部内容，仅受安全上限约束。

        排序：适合你启动指数 → 综合分 → 商业化潜力。
        """
        kept = [it for it in items if _is_real_opportunity(it, min_startup_index)]
        kept.sort(
            key=lambda x: (
                getattr(x, "startup_index", 0) or 0,
                x.relevance_score or 0,
                getattr(x, "commercial_score", 0) or 0,
            ),
            reverse=True,
        )
        # per_region 只是安全护栏，不是硬目标；优质内容不封顶
        return kept[:per_region]
