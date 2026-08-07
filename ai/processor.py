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

## 🚫 卖铲子 / 割韭菜检测（非常重要！）

真正赚钱的方法几乎没有人会免费公开。互联网上绝大多数"教你赚钱"的内容本质上是「卖铲子」——通过告诉你一个赚钱的方法，真正目的是卖给你这个方法所需的工具、课程、服务、社群。

**卖铲子的典型特征（必须识别）：**
1. 文章结尾或中间有「加我微信」「扫码进群」「购买课程」「使用我的链接」「用这个工具（带推广码）」→ **立即标记为卖铲子，relevant=false**
2. 只告诉你「能赚钱」但不说具体怎么做 → 让你好奇 → 引导你付费 → **relevant=false**
3. 「我靠XX月入10万，你也可以」→ 只讲结果不讲过程 → **relevant=false**
4. 「99元学会XX」「限时优惠」「名额有限」→ **relevant=false**
5. 标题夸张但正文空洞：「震惊！XXX竟然能月入五万」→ **relevant=false**

**不是卖铲子的特征（可以推送）：**
1. 完整记录了从0到1的每一步，没有付费墙 → 真分享
2. 作者不需要你买任何东西就能看完 → 真分享
3. 内容被其他读者在评论区验证过、讨论过 → 有公信力
4. 作者公开了收入截图、具体数据 → 可信度高

**判断口诀**：
- 文章让你想掏钱 → 卖铲子 → irrelevant
- 文章让你想动手试试 → 真分享 → 值得推

---

## 📊 平台/来源可靠性评估

不同来源的可信度差异巨大。请在判断 relevance_score 时考虑来源：

### 高可信（加分 +0.05~0.1）
- 公众号/个人博客深耕多年，有持续产出 → 可信
- 论坛帖子被大量讨论验证过（评论≥20条且多数正面）→ 可信
- 小宇宙播客/独立播客的文字稿 → 通常是深度分享
- 即刻/Reddit/Twitter 上的个人经验帖，有具体数字和过程 → 可信

### 中可信（不加不减）
- 知名独立博客（阮一峰、少数派作者等）→ 内容质量稳定
- V2EX/Reddit 热帖 → 有社区验证但不能全信

### 低可信（减分 -0.1~0.2）
- 知乎专栏文章 → 任何人都能写，营销号重灾区
- 公众号新号/低粉号 → 无法判断可信度
- 标题党、纯SEO内容 → 大概率是流水线生产的

### 基本不可信（直接 irrelevant）
- 搜索引擎排名靠前但内容空洞的 → SEO垃圾
- 没有作者署名、没有日期的 → 机器人写的
- 大媒体（36氪/虎嗅等）的「行业趋势」「XX赛道分析」→ 落后于市场，没有实操价值

---

## ⏰ 信源新鲜度分级

**信息传播链条（先到后）**：
个人发现/小范围讨论 → 小媒体/垂直社区 → 自媒体跟进 → 大媒体报道

**越靠前信号越有价值**：
- 个人在即刻/Twitter/Reddit/V2EX首发的发现 → 最高价值（+0.1 bonus）
- 小媒体/垂直社区（公众号、Newsletter、播客）的深度分析 → 高价值
- 自媒体（YouTube/B站/抖音）的案例拆解 → 中等价值（已验证过一轮）
- 大媒体（36氪、TechCrunch等）的报道 → 低价值（已是旧闻）

**媒介形式的时间差**：
- 短文字（帖子/推文/即刻）→ 最早发现
- 长文章（博客/公众号）→ 系统性梳理（晚1-3天）
- 视频（YouTube/B站/抖音）→ 最晚（晚3-7天）

**判断标准**：如果同一个话题已经在多个地方看到过 → 已经是旧闻，价值降低 0.05~0.1。

---

## 🧠 商业可行性判断

你需要在总结中加入你自己的商业判断。不是说「这个好」或「这个不好」，而是具体说：
1. 这个模式在中国的环境下能做吗？（中外国情差异）
2. 启动成本大概多少？（时间 + 金钱）
3. 最大的风险是什么？（平台封号？竞争激烈？需求太窄？）
4. 如果模仿，第一步应该做什么？

这些判断写在 summary 的最后，用「💭 我的判断：」开头。

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

### 🚫 卖铲子/营销
- 任何引导你付费的内容 → irrelevant
- 标题夸张、正文空洞的 → irrelevant
- 「加我微信」「扫码进群」「限时优惠」→ irrelevant

---

## 输出格式
返回严格 JSON 数组：

[
  {
    "index": 0,
    "relevant": true,
    "translation": "中文翻译（英文必译，中文留空）",
    "summary": "文章大意（80-120字）：用大白话把文章内容说清楚。假设你在跟你妈解释这篇文章讲了什么。包括：这个人做了件什么事？他怎么做起来的？第一步是什么？赚了多少？用了多久？有什么普通人也可以模仿的地方？最后加一段 💭 我的判断：说说这个模式在中国能不能做、启动成本、最大风险。记住：不许出现SaaS/MRR/SEO/Affiliate/niche/引流等术语，全部用括号里的说法替代。",
    "opportunity_hint": "如果你是一个不会用电脑的普通人，怎么模仿这个赚钱方法。用大白话说清楚：卖什么 × 去哪卖 × 怎么找顾客 × 收多少钱。30-40字。禁止出现任何编程/技术术语。",
    "difficulty": "零门槛" 或 "需学习" 或 "有一定门槛",
    "quality_flag": "⭐" 或 "" 或 "⚠️",
    "relevance_score": 0.85
  },
  {
    "index": 1,
    "relevant": false,
    "reason": "一句话原因"
  }
]

## quality_flag 说明
- **⭐** = 高价值信号：小渠道首次提到 / 细节非常丰富 / 公众号深度分享 / 有大量社区验证
- **""** = 正常内容，无明显风险也无特别亮点
- **⚠️** = 有风险信号：来源是知乎专栏 / 标题党 / 内容空洞 / 可能是营销号 / 有卖铲子嫌疑但还不够直接淘汰

## difficulty 说明
- **零门槛**：不需要学任何新工具，直接用手机/电脑就能开始
- **需学习**：需要花几天了解一个新平台或新技能
- **有一定门槛**：需要一些资金投入、或者需要花较长时间学习

## 评分标准（综合可靠性+新鲜度+实操性）
- **0.8-1.0**：普通人能做 + 具体过程 + 高可信来源 + 可能有首发红利 → 必推
- **0.6-0.7**：适合普通人的赚钱方法，有实操过程，来源可信
- **0.5**：有参考价值但门槛偏高/来源不太可信/已是旧闻
- **0.0-0.4**：不满足三问 / 卖铲子 / 不可信 → irrelevant

## 严格要求
1. 纯 JSON 数组，不要 markdown 包裹
2. 每条都要有 index
3. 英文必翻译成中文
4. 术语全部用中文大白话替换
5. summary 不能出现任何英文缩写
6. 卖铲子必须 relevant=false
7. score<0.4 必须 relevant=false
8. irrelevant 的也要返回，不要省略"""



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
                        item.quality_flag = r.get("quality_flag", "")
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
