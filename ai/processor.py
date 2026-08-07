"""
AI 处理层 — 批量翻译、总结、赚钱机会挖掘。

核心改进：一次 API 调用处理全部内容，彻底解决逐条调用导致的限速问题。
只推送经过 AI 处理且得分 > MIN_SCORE 的内容。
"""

import json
import logging
import re

import httpx

from sources.base import ContentItem

logger = logging.getLogger(__name__)

MIN_SCORE = 0.5  # 最低推送门槛（提高：必须三问至少通过两个）

BATCH_SYSTEM_PROMPT = """你是一个「一人公司 (OPC) 赚钱机会挖掘」分析助手。
你的唯一使命：找出「一个人 + AI 工具，现在就能起步做的赚钱机会」。

## 核心筛选三问（每条内容必须全部通过）
对每条内容，回答三个问题：
1. **一个人能做吗？** 不需要团队、融资、大公司资源 → 是/否
2. **AI 能加速吗？** 用 Cursor/ChatGPT 等工具能大幅降低门槛 → 是/否
3. **有第一步吗？** 内容给出了具体起步方式（做什么/服务谁/哪获客/定什么价）→ 是/否

三问全通过 → relevant=true。任一不通过 → relevant=false。

## 什么是真正可操作的 OPC 机会（应该推）
- 「我用 AI 花 2 周做了 XX 插件，月入 $3K」→ 具体产品+方法+收入，可模仿
- 「这个 niche（XX工具）搜索量 10 万，还没人做 SaaS」→ 需求洞见
- 「在 Gumroad/Etsy 卖 Notion 模板月入 $2K，怎么做」→ 具体方法
- 「Reddit 上这个版天天有人求 XX 工具」→ 获客线索
- 「零成本通过 SEO/Reddit/ProductHunt 把 Micro-SaaS 做到 $5K MRR」→ 增长方法
- 适合一个人做的具体小生意点子（niche 明确、操作门槛低）

## 什么不是（必须标为 irrelevant）
- 大公司融资/上市/并购新闻 → 一个人做不了
- 「XX 公司 $80M 退出」「XX 估值 $X 亿」→ 结果报道，无方法
- 行业趋势/宏观分析报告 → 没有具体可做的事
- 科技产品发布/评测 → 产品新闻
- 个人感悟/生活/娱乐内容
- 泛泛的「创业要专注」「成功人士的 10 个习惯」→ 鸡汤，无方法
- 单纯的技术教程（跟赚钱无关）
- 别人做大的成功故事但没有可复制的方法

## 输出格式
返回严格 JSON 数组：

[
  {
    "index": 0,
    "relevant": true,
    "translation": "中文翻译（英文必译，中文留空）",
    "summary": "谁用什么方法做了什么产品/服务，赚了多少/增长如何，40字",
    "opportunity_hint": "具体可操作的起步建议：做什么产品、服务什么人、在哪获客。必须包含具体 niche/平台/方法，不能笼统。25字",
    "relevance_score": 0.85
  },
  {
    "index": 1,
    "relevant": false,
    "reason": "一句话说明为什么不符合三问（如：需团队、无方法、纯新闻）"
  }
]

## 评分标准（严格）
- 0.8-1.0: 明确的一人+AI 可做的赚钱案例/方法，有具体数字和步骤
- 0.6-0.7: 有参考价值的案例或方法，但缺少部分实操细节
- 0.4-0.5: 与独立创业相关但偏理论/宏观，可操作性一般
- 0.0-0.3: 不满足三问 → 直接标 irrelevant

## opportunity_hint 要求（关键）
必须具体到「做什么产品 × 服务什么人群 × 在哪获客」三要素至少包含两个。
❌ 不合格：「做 SaaS 赚钱」「内容变现」「搞跨境电商」「做 AI 工具」
✅ 合格：
  「在 Gumroad 卖婚礼策划 Notion 模板，从婚礼 subreddit 引流」
  「用 AI 做 Chrome 插件自动写 LinkedIn 评论，freemium 定价 $9/月」
  「找一个团队日常必用的小痛点，做开源+托管付费的 Micro-SaaS」
  「在 ProductHunt 和 Hacker News 发布 AI 效率工具，收集 waitlist」

## 严格要求
1. 纯 JSON 数组输出，不要 markdown 包裹
2. 每条都要有 index，不要遗漏
3. 英文内容必须翻译成中文放在 translation 字段
4. relevance_score 和 relevant 必须一致（score<0.4 → relevant=false）
5. irrelevant 的也要返回，不要省略"""


class AIProcessor:
    """AI 内容处理器（批量模式）。"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 max_tokens: int = 4000, temperature: float = 0.2):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._enabled = bool(
            api_key
            and not api_key.startswith("YOUR_")
            and not api_key.startswith("sk-你的")
            and "你的" not in api_key
            and len(api_key) > 20
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def process(self, items: list[ContentItem]) -> list[ContentItem]:
        """批量处理全部内容。一次 API 调用处理所有条目。"""
        if not self._enabled or not items:
            return items

        # 构建批量输入：每条内容包含 index + 标题 + 摘要
        input_lines = []
        for i, item in enumerate(items):
            title = item.title[:120]
            summary = item.summary[:200] if item.summary else "(无摘要)"
            input_lines.append(f"[{i}] {title} | {summary}")
        user_content = "\n".join(input_lines)

        logger.info(f"批量处理 {len(items)} 条内容...")

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": BATCH_SYSTEM_PROMPT},
                            {"role": "user", "content": user_content},
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # 解析 JSON
                results = self._parse_batch_response(content)

                # 应用到原始条目
                result_map = {r["index"]: r for r in results if isinstance(r, dict)}
                for i, item in enumerate(items):
                    r = result_map.get(i)
                    if r and r.get("relevant"):
                        item.translation = r.get("translation", "")
                        item.ai_summary = r.get("summary", "")
                        item.opportunity_hint = r.get("opportunity_hint", "")
                        if isinstance(r.get("relevance_score"), (int, float)):
                            item.relevance_score = r["relevance_score"]
                        item.ai_processed = True
                    else:
                        # 标记为不相关，后续会被过滤
                        item.relevance_score = 0.0
                        item.ai_processed = True

            return items

        except Exception as e:
            logger.error(f"批量 AI 处理失败: {e}")
            # 失败时标记所有条目为未处理，后续会被过滤
            for item in items:
                item.ai_processed = False
            return items

    def _parse_batch_response(self, content: str) -> list[dict]:
        """解析批量 AI 响应。"""
        content = content.strip()

        # 去除可能的 markdown 代码块包裹
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 数组
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        logger.error(f"无法解析 AI 响应: {content[:500]}")
        return []
