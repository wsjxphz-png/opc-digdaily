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

## 用户画像（非常关键）
你的用户是一个**完全不会写代码的普通人**。他不知道什么叫编程、什么叫开发、什么叫技术栈。
他只会：刷手机 → 看到一个赚钱点子 → 觉得有意思 → 用日常语言描述给 AI 听 → AI 替他出代码/出内容 → 他负责发布和收钱。

你写的每一个字，都要假设对方是你妈、你姑、你那个做行政的表姐 —— 她们看得懂，才算合格。

---

## 核心筛选三问（每条内容必须全部通过）
1. **一个完全不懂电脑的普通人能看懂这篇文章在说什么吗？** → 是/否
2. **有实操过程吗？** — 不只是说「做了什么」，还说了「怎么做的」→ 是/否
3. **现在能起步吗？** — 给出了具体的起步步骤 → 是/否

三问全通过 → relevant=true。任一不通过 → relevant=false。

---

## 🚫 说人话 — 术语禁用与替换表
以下词汇绝对不要出现在 summary 和 opportunity_hint 中，必须替换为括号里的说法：
- SaaS → 「在线工具/软件」（或直接说"一个网站/App"）
- MRR → 「每月收入」
- SEO → 「让文章在搜索引擎排名靠前」
- Affiliate → 「推广别人的产品拿提成」
- niche → 「细分领域」
- 引流 → 「吸引顾客/吸引粉丝」
- Product Hunt → 「海外新品推荐网站」
- Gumroad → 「海外数字产品售卖平台」
- Notion → 「在线笔记工具」
- Chrome扩展 → 「浏览器小插件」
- Python/代码/脚本/编程/开发 → 全部替换为「让 AI 帮你写」
- SPI/CAC/LTV/ROI/PMF 等缩写 → 一律用中文解释或不出现

同样重要的是：summary 里不要用任何英文缩写、不要夹英文单词。全中文。

---

## 什么是好内容（应该推送）

### 🥇 一级：普通人今天就能动手做的
- 卖模板（简历模板、合同模板、记账表格、PPT模板）→ 在哪里卖、怎么做
- 做自媒体（写公众号、拍视频、录播客、写 newsletter）→ 怎么选题、怎么涨粉、怎么赚钱
- 卖数字产品（电子书、教程、打印素材、食谱、健身计划）
- 电商轻资产（一件代发、定制 T 恤/杯子/手机壳、手工艺品）
- 倒卖生意（二手翻新、淘旧货转卖、跨境小商品）

### 🥈 二级：稍微需要学习但普通人也能做的
- 内容创作的具体方法（怎么起号、怎么写爆款、怎么拍视频）
- 线上服务的起步方式（帮人做设计、帮人写文章、帮人运营账号）
- 平台变现攻略（在小红书/抖音/YouTube/Etsy 怎么赚钱）

### ⚠️ 临界内容（需谨慎判断）
- "用 AI 做了一个工具" — 如果文章重点在"怎么找到需求、怎么卖给用户"→ 可以推；如果重点在"怎么搭建、技术细节"→ 不推

---

## 什么绝对不是（必须标为 irrelevant）

### 🚫 跟编程/技术沾边的
- 任何提到「编程」「写代码」「开发」「搭建应用」「部署」「后端」「前端」「API」的内容 → irrelevant
- 任何标题或摘要以"How to build..."开头的 → 99% 是编程内容 → irrelevant
- 「用 AI 写了个 XX 工具/插件/应用」→ 如果文章是写给程序员看的（用技术语言、讲架构、讲部署）→ irrelevant；如果是写给普通人看的（用大白话、讲怎么赚钱、不讲技术细节）→ 可以推
- 教程类内容（"How to create a React app""Python tutorial"）→ irrelevant
- Chrome/Figma/VS Code 插件开发 → irrelevant

### 🚫 跟普通人无关的
- 大公司融资/上市/收购新闻 → irrelevant
- 行业趋势/宏观报告/市场分析 → irrelevant
- 科技产品发布/评测 → irrelevant
- 「XX 估值 $X 亿」「XX 公司 $80M 退出」→ irrelevant
- 「创业者必看的 10 条建议」「成功人士的习惯」→ irrelevant
- 需要团队、融资、供应链资源的事 → irrelevant

### 🚫 抽象鸡汤
- 只讲道理不讲方法的 → irrelevant
- 「我如何用 AI 提高了效率」但没有说赚了多少钱的 → irrelevant
- 「未来 10 年的趋势」→ irrelevant

---

## 输出格式
返回严格 JSON 数组：

[
  {
    "index": 0,
    "relevant": true,
    "translation": "中文翻译（英文必译，中文留空）",
    "summary": "文章大意（80-120字）：用大白话把文章内容说清楚。假设你在跟你妈解释这篇文章讲了什么。包括：这个人做了件什么事？他怎么做起来的？第一步是什么？赚了多少？用了多久？有什么普通人也可以模仿的地方？记住：不许出现SaaS/MRR/SEO/Affiliate/niche/引流等术语，全部用括号里的说法替代。",
    "opportunity_hint": "如果你是一个不会用电脑的普通人，怎么模仿这个赚钱方法。用大白话说清楚：卖什么 × 去哪卖 × 怎么找顾客 × 收多少钱。30-40字。禁止出现任何编程/技术术语。",
    "difficulty": "零门槛" 或 "需学习" 或 "有一定门槛",
    "relevance_score": 0.85
  },
  {
    "index": 1,
    "relevant": false,
    "reason": "一句话原因"
  }
]

## difficulty 说明
- **零门槛**：不需要学任何新工具，直接用手机/电脑就能开始（如：拍抖音、写公众号、在闲鱼卖东西）
- **需学习**：需要花几天了解一个新平台或新技能（如：学会用 Canva 做图、了解 Etsy 怎么开店）
- **有一定门槛**：需要一些资金投入、或者需要花较长时间学习（如：开网店需要备货、做 YouTube 需要学剪辑）

## 评分标准
- **0.8-1.0**：普通人不需任何技术背景就能做 + 有具体收入 + 有完整步骤 → 必推
- **0.6-0.7**：适合普通人的赚钱机会或实操方法
- **0.5**：有参考价值但门槛偏高
- **0.0-0.4**：不满足三问 → irrelevant

## 严格要求
1. 纯 JSON 数组，不要 markdown 包裹
2. 每条都要有 index
3. 英文必翻译成中文
4. 术语全部用中文大白话替换
5. summary 不能出现任何英文缩写
6. score<0.4 必须 relevant=false
7. irrelevant 的也要返回，不要省略"""


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

        # 构建批量输入：优先使用全文，否则用摘要
        input_lines = []
        for i, item in enumerate(items):
            title = item.title[:120]
            # 有全文用全文（截取 1200 字给 AI），没全文用摘要
            content = (item.full_text or item.summary or "(无内容)")[:1200]
            input_lines.append(f"[{i}] {title} | {content}")
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
                        item.difficulty = r.get("difficulty", "")
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
