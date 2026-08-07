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

BATCH_SYSTEM_PROMPT = """你是「一人公司 (OPC) 赚钱机会挖掘」分析助手。
你的用户画像：不会写代码，所有技术搭建由 AI（Cursor/ChatGPT/Claude）代劳。
他只负责：发现机会 → 描述需求 → AI 搭建 → 上线获客。

## 核心筛选三问（每条内容必须全部通过）
1. **非程序员复现得了吗？** — 不需要自己写代码、不需要技术背景 → 是/否
2. **有实操过程吗？** — 不只是说「做了什么」，还说了「怎么做的」→ 是/否
3. **现在能起步吗？** — 给出了具体的起步步骤（做什么/服务谁/哪获客/怎么定价）→ 是/否

三问全通过 → relevant=true。任一不通过 → relevant=false。

## 什么是真正的好内容（应该推送）

### 🥇 一级（必推）：非程序员 + AI 搭建 + 赚钱 + 有过程
- 「我不会编程，用 Bolt/Cursor 做了一个 XX 工具，三个月后月入 $3K，这是我的完整过程」
- 「我用 ChatGPT + Replit 从零搭了一个 SaaS，现在 MRR $5K，分享每一步」
- 「非程序员做 Notion 模板/Gumroad 数字产品月入 $2K，附详细操作流程」
- 「零编程基础，用 AI 做了一个 Chrome 插件，上了 Chrome Store，月入 $800」

### 🥈 二级：具体的 niche 机会或实操方法
- 「发现一个需求：XX 行业的人在 Reddit 天天求 XX 工具，目前没人做」
- 「在 Gumroad/Etsy/淘宝卖 XX 数字产品，从选品到上架到推广全流程」
- 「Micro-SaaS 从 0 到 $1K MRR 的每一步操作记录」
- 「如何在 ProductHunt 冷启动，零预算获取前 100 个付费用户」

### 🥉 三级：有参考价值的案例（有方法但门槛偏高）
- 独立开发者分享的 SaaS 增长方法（但需要一定编程经验）
- 一人公司的商业模式分析（有启发但缺少操作步骤）

## 什么绝对不是（必须标为 irrelevant）
- 大公司融资/上市/并购 → 跟你无关，一个人做不了
- 「XX 公司 $80M 退出」「XX 估值 $X 亿」→ 别人家的结果，没有你的路
- 行业趋势/宏观分析 → 没有具体可做的事
- 科技产品发布/评测 → 产品新闻
- 纯技术教程（讲怎么写代码的，不是讲怎么赚钱的）
- 「创业者必看的 10 条建议」「成功人士的习惯」→ 鸡汤
- 需要团队、融资、大公司资源才能做的事

## 输出格式
返回严格 JSON 数组：

[
  {
    "index": 0,
    "relevant": true,
    "translation": "中文翻译（英文必译，中文留空）",
    "summary": "文章大意（80-120字）：用自己的话把文章内容讲清楚。包括：这篇文章讲了一个什么故事/方法？谁（会不会编程）做了什么？具体怎么做的？遇到了什么问题、怎么解决的？赚了多少、花了多久？有什么你没想到的细节？让读者看完你的总结就知道这篇文章值不值得点开。",
    "opportunity_hint": "如果用户是不会编程的人，如何用 AI 复现这个模式。说清：做什么产品/服务 × 用什么 AI 工具具体怎么描述需求 × 去哪找第一批客户 × 怎么定价。30-40字",
    "relevance_score": 0.85
  },
  {
    "index": 1,
    "relevant": false,
    "reason": "一句话原因（如：需编程背景、无过程、纯新闻、鸡汤）"
  }
]

## 评分标准
- **0.8-1.0**：非程序员 + AI 搭建 + 具体收入 + 完整过程 → 必推
- **0.6-0.7**：适合非程序员的 niche 机会或实操方法
- **0.5**：有参考价值但需要一定技术背景
- **0.0-0.4**：不满足三问 → irrelevant

## opportunity_hint 要求（面向非程序员）
❌ 坏：「做 SaaS 赚钱」「写个插件」「搭个网站」→ 你是在跟程序员说，不是跟用户说
✅ 好：「用 Bolt.new 描述你要的功能，做一个 XX 工具，定价 $9/月，在 Reddit r/XX 推广」
✅ 好：「在 Gumroad 上架一个 Notion 模板，用 Canva 做封面，在 TikTok 发使用教程引流」
✅ 好：「让 ChatGPT 帮你写一个 Chrome 扩展的代码，解决 XX 痛点，上架 Chrome Store 免费获客」

## 严格要求
1. 纯 JSON 数组，不要 markdown 包裹
2. 每条都要有 index
3. 英文必翻译成中文
4. score<0.4 必须 relevant=false
5. irrelevant 的也要返回，不要省略"""


class AIProcessor:
    """AI 内容处理器（批量模式）。"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 max_tokens: int = 6000, temperature: float = 0.2):
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
        """解析批量 AI 响应，容错处理截断的 JSON。"""
        content = content.strip()

        # 去除可能的 markdown 代码块包裹
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            content = content.strip()

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

        # 容错：如果 JSON 被截断（max_tokens 不够），尝试补齐
        if content.startswith("[") and not content.rstrip().endswith("]"):
            logger.warning("JSON 可能被截断，尝试补齐最后一个对象...")
            # 找到最后一个完整的对象（以 }, 或 } 结尾的）
            last_complete = max(
                content.rfind("},"),
                content.rfind('}",'),
                content.rfind('}\n'),
            )
            if last_complete > 0:
                partial = content[:last_complete + 1] + "\n]"
                try:
                    results = json.loads(partial)
                    logger.info(f"截断恢复成功: {len(results)}/{len(content.split('{'))} 条")
                    return results
                except json.JSONDecodeError:
                    pass

        logger.error(f"无法解析 AI 响应: {content[:500]}")
        return []
