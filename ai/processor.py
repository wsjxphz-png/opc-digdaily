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

MIN_SCORE = 0.4  # 最低推送门槛

BATCH_SYSTEM_PROMPT = """你是一个「一人公司 (OPC) 赚钱机会挖掘」分析助手。
你的受众是想找到真实赚钱机会的独立创业者、solopreneur。

对于下面每一条内容，判断它是否真正与「一人公司如何赚钱」相关，然后按格式输出。
如果内容只是泛泛的科技新闻、产品发布、行业分析跟赚钱无关，直接标为 irrelevant。

## 相关标准（判断不是简单关键词匹配，要看实质）
真正相关：
- 某人/某个一人公司是如何赚到钱的（收入数字、案例）
- 适合一人/小团队做的赚钱项目或商业模式
- 独立开发者/小团队发布的产品，有收入/用户数据
- 副业、被动收入的实操方法
- Micro-SaaS / bootstrapped 产品的经验分享
- 数字游民/自由职业者的收入方法论

不相关（直接标 irrelevant）：
- 大公司融资/上市新闻
- 普通科技产品评测/导购
- 游戏/影视/娱乐资讯
- 宏观经济/行业研究报告
- 单纯的技术教程（跟赚钱无关）
- 个人生活/感悟类内容
- 新闻式的"XX发布了XX"（没有收入/用户数据）

## 输出格式
返回严格 JSON 数组，每条内容一个对象：

[
  {
    "index": 0,
    "relevant": true,
    "translation": "中文翻译（英文内容必须翻译;原文是中文则留空）",
    "summary": "一句话中文总结（什么项目/什么人/赚了多少/怎么赚的），40字以内",
    "opportunity_hint": "具体可操作的一人公司赚钱机会或商业启发，25字以内，没有则写'无'",
    "relevance_score": 0.85
  },
  {
    "index": 1,
    "relevant": false,
    "reason": "不相关原因（5字）"
  }
]

评分标准:
- 0.8-1.0: 明确的一人公司赚钱案例/项目/方法（有收入数字）→ 每条推送必须优先
- 0.5-0.7: 与一人公司创业相关但无具体赚钱方法
- 0.3-0.4: 略有相关但很边缘
- 0.0-0.2: 几乎不相关 → 直接标 irrelevant

严格要求：
1. 输出必须是纯 JSON 数组，不要包裹在 markdown 代码块里
2. 不要遗漏任何 index
3. 英文内容必须翻译成中文
4. opportunity_hint 要具体，避免"做SaaS赚钱"这种空泛描述"""


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
