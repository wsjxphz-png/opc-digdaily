"""
AI 处理层 — 翻译、总结、赚钱机会挖掘。

调用 OpenAI 兼容接口，支持任意模型 (GPT-4o-mini / DeepSeek / 本地模型等)。
"""

import asyncio
import json
import logging
import re

import httpx

from sources.base import ContentItem

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个「一人公司 (OPC: One Person Company) 赚钱机会挖掘」分析助手。你的目标受众是想找到赚钱机会的独立创业者、数字游民、solopreneur 和斜杠青年。

你熟悉以下领域的英文术语和缩写：OPC, SaaS, Micro-SaaS, MRR, ARR, PMF, CAC, LTV, PLG, MVP, SEO, CRO, ROI, B2B, B2C, D2C, POD, ICP, WFH, LLM, GPT 等。

对于每篇提供的内容（可能是英文或中文），你需要完成以下分析，并以**严格的 JSON 格式**返回：

{
  "translation": "中文翻译（英文/外文内容必须翻译成中文，原文是中文则留空字符串）",
  "summary": "用一句话总结核心内容（中文，50字以内）",
  "opportunity_hint": "具体可操作的赚钱机会或商业启发（中文，30字以内，没有则写'暂无明确机会'）",
  "relevance_score": 0.0
}

relevance_score 规则 (0~1):
- 明确提到赚钱方法、商业模式、变现思路、收入数据(MRR/ARR) → 0.8~1.0
- 与创业、副业、独立开发、数字游民、个人成长相关但无直接赚钱方法 → 0.5~0.7
- 略有相关但无实用价值 → 0.2~0.4
- 完全无关 → 0.0

注意：英文内容务必翻译成中文。保持 opportunity_hint 具体、可操作，避免空泛建议。"""


class AIProcessor:
    """AI 内容处理器。"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 max_tokens: int = 800, temperature: float = 0.3):
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

    async def process(self, items: list[ContentItem],
                      concurrency: int = 1) -> list[ContentItem]:
        """批量处理内容条目。"""
        if not self._enabled:
            logger.warning("AI 未配置，跳过处理")
            return items

        semaphore = asyncio.Semaphore(concurrency)

        async def _process_one(item: ContentItem) -> ContentItem:
            async with semaphore:
                result = await self._process_single(item)
                await asyncio.sleep(0.5)  # 避免触发 API 限速
                return result

        tasks = [_process_one(it) for it in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"AI 处理失败 [{items[i].title[:30]}]: {result}")
                processed.append(items[i])
            else:
                processed.append(result)

        return processed

    async def _process_single(self, item: ContentItem) -> ContentItem:
        """处理单条内容。"""
        text = f"标题: {item.title}\n\n内容: {item.summary}"
        if len(text) > 1500:
            text = text[:1500]

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text},
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                content = data["choices"][0]["message"]["content"]
                # 去除可能的 markdown 代码块包裹
                content = content.strip()
                if content.startswith("```"):
                    # 去除 ```json 和末尾 ```
                    content = re.sub(r"^```(?:json)?\s*", "", content)
                    content = re.sub(r"\s*```$", "", content)
                result = json.loads(content)

                item.translation = result.get("translation", "")
                item.ai_summary = result.get("summary", "")
                item.opportunity_hint = result.get("opportunity_hint", "")

                ai_score = result.get("relevance_score", 0.0)
                if isinstance(ai_score, (int, float)):
                    item.relevance_score = max(item.relevance_score, ai_score)
            return item
        except Exception as e:
            raise
