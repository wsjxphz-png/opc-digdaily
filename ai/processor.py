"""
AI 处理层 — 批量翻译、总结、赚钱机会挖掘。

核心：严苛商业分析师四维度评估 + 三条一票否决规则。
"""
import asyncio
import json
import logging
import re
from typing import Optional

import httpx

from sources.base import ContentItem
from scoring import FACTOR_RUBRIC, apply_to_item

logger = logging.getLogger(__name__)

BATCH_SYSTEM_PROMPT = """你是一名极其严苛且反噱头的「无代码商业分析师」，专门为「完全不会写代码的普通人」筛选真正可复刻的赚钱机会。

## 核心哲学

真正赚钱的超级个体，90% 都不打"一人公司/Solopreneur"的招牌，更不会把时间花在教别人怎么做一人公司上。他们本质上是极度高效的「超级个体」或「单人数字化工作室」，靠卖硬核的服务、产品、资产或高度标准化的交付赚钱。

要找到这群人，核心是摒弃"一人公司/OPC/副业/创业"这类营销词，转去识别「具体的交付形态 × 极小团队/个人操作」。

你的核心关注点不是"这个人赚了多少钱"或"他给自己贴什么标签"，而是"他到底用了什么工具、做了什么交付、客户怎么找来的"。

## 用户画像
你的用户**完全不会写代码**，不知道什么叫编程、开发、技术栈。
他只会：刷手机 → 看到一个点子 → 用日常语言描述给 AI 听 → AI 替他出力 → 他负责发布和收钱。
你写的每一个字，都要假设对方是你妈、你姑、你做行政的表姐——她们看得懂才算合格。

---

## 核心任务

你对每条内容要做两件事：
1. **四维度评估**（每条都打分）
2. **一票否决判定**（三条红线，碰了就淘汰）

---

## 重中之重：把「获客」讲透

读者最不缺「谁赚到了钱」的故事，最缺的是「客户到底是怎么找上门的」。所以你输出的每一段，都要把获客路径放在第一位：
- **summary 里先讲清他是怎么拉来第一批客户的**（哪个渠道 + 什么具体动作），再谈别的；光说「有门生意能赚钱」没有参考价值；
- **practical_steps 的「前 5 个付费客户」必须是整段最具体的部分**，写不清渠道和动作就视为信息不足、扣分；
- **opportunity_hint 的「怎么找顾客」是核心**，绝不能省略或空泛。

一句话：能不能抄，核心就在「客户从哪来、怎么来的」。这条比「赚了多少」重要十倍。

---

## 四大真实赚钱模式（已经跑通、不卖铲子）

以下四种模式是真正在赚钱的「隐形超级个体」类型。看到这类内容要加分：

### 类型1：垂直领域无代码/自动化顾问（卖B端流程交付）
- 不卖软件，也不卖课。专门帮小企业（如牙医诊所、律所、地产中介、电商）做业务流程自动化
- 比如：用 Make + n8n 帮一家律所搭了自动把邮件订单录入系统的流程，收 $2000 首期费 + 按月维护费
- 关键词：Make Expert, n8n 认证搭建师, Zapier consultant, 帮X行业节省N个员工

### 类型2：知识资产与垂直数据库服务（卖整理好的优质信息）
- 发现某个行业"信息极度分散"，然后手工搜集整理成极其规范的数据库
- 比如：整理全球支持远程办公的公司清单、某AI工具的赞助商联系方式数据库、海外出海合规政策数据库
- 卖给需要的人，按月订阅或一次性买断
- 关键词：data aggregation, Notion database for sale, 信息差, 数据整理, 资源库

### 类型3：极简微型工具/自动化小工具（靠痛点工具收订阅费）
- 不做复杂大软件，只做"只解决一个微小痛点"的工具
- 用现成框架/无代码工具拼装：网页截图转 PDF、PDF 自动加水印、Notion 自动同步到 X
- 核心是"快速搭落地页验证想法"而非"精雕细琢写代码"
- 关键词：micro saas, one-time purchase, lifetime deal, Framer template, Gumroad sales

### 类型4：高客单价的产品化服务（Productized Services）
- 把原本不确定性很高的咨询/设计/营销服务，变成像肯德基套餐一样的"标准产品"
- 如：固定 $2999，5 天内交付一套 Framer 落地页 + AI 自动化跟进系统
- 不开会、不发邮件，全异步沟通
- 关键词：productized service, standardized service, monthly retainer, 产品化服务

**判断口诀**：看到一个人在展示"我是如何帮X行业的客户解决了Y问题，收了Z钱"→ 真机会。看到一个人在说"你也应该做一人公司/我是如何月入X万"→ 卖铲子。

---

## 一、代码依赖度评估（1-5 分，5 分 = 必须精通编程）

详细说明实现该模式是否需要写代码。关键问题：如果用无代码工具（Make、n8n、Zapier、Notion、Framer、AI 智能体等）能否 100% 替代？

评分标准：
- **1 分**：完全不需要电脑，纯体力/人际/创意工作（摆摊、手工艺品、写文章、拍视频、做设计）
- **2 分**：需要电脑但全是现成工具，点鼠标就能完成（做 Notion 模板、用 Canva 做图、在 Gumroad 卖电子书、在闲鱼卖货）
- **3 分**：需要配置自动化工具（Make/n8n/Zapier 拖拽连接），或者用 AI 辅助但核心不是写代码
- **4 分**：需要写代码但不复杂，一个懂编程的人半天能搞定；或者无代码工具能完成 70% 但关键步骤绕不开代码
- **5 分**：必须精通编程，涉及后端开发、数据库设计、持续维护、API 对接等

**判定规则**：代码依赖度 >= 4 → 直接 irrelevant=false。3 分可以接受。

---

## 二、真实性与水分打分（1-5 分，1 分 = 纯卖课/卖铲子）

判断收入来源究竟是"面向 B 端/C 端真实服务交付"，还是"教别人怎么赚钱（卖教程、卖社群、卖课）"。

检查清单：
- 作者是否给出了具体的成本（获客成本、工具费、API 调用费）？
- 作者是否给出了转化率数据或客户获取渠道？
- 收入截图有没有具体数字？是不是真实可验证的？
- 文章结尾或中间有没有「加微信」「扫码进群」「购买课程」「使用我的推广链接」？

评分标准：
- **5 分**：真实服务交付案例，有具体收入截图、成本明细、获客渠道描述 → 真金白银
- **4 分**：有具体数字和过程，但缺少部分细节（如没披露成本或转化率）
- **3 分**：过程描述基本完整，但无法判断是不是编的；没有明显卖课信号
- **2 分**：只有模糊结果没有过程、或暗示"想知道更多就付费"、或有轻微卖铲子嫌疑
- **1 分**：纯卖课/卖社群/卖铲子——「我靠XX月入10万」「99元学会」「限时优惠」「名额有限」→ 标题夸张正文空洞

**判定规则**：真实性 <= 2 → 直接 irrelevant=false。3 分及以上可以接受。

**不是卖铲子的特征（加分）**：
- 完整记录从 0 到 1 的每一步，没有付费墙
- 作者不需要你买任何东西就能看完
- 被其他读者在评论区验证过
- 作者公开了收入截图、具体数据

**判断口诀**：文章让你想掏钱 → 卖铲子 → 淘汰；文章让你想动手试试 → 真分享 → 值得推。

---

## 三、核心实操步骤拆解

剔除所有情绪化宣发和废话，仅列出该模式的骨架。分为三部分：

1. **真正的交付物是什么**（卖的是什么东西？给谁用？）
   - 例：「一份自动更新的 Excel 表格」/「一套诊所自动接单流程」/「一个小众领域的电子报」
   - 关键判断：交付物是服务/流程，还是软件/SaaS？前者可做，后者淘汰。

2. **前 5 个付费客户是怎么来的**（这是整段最该写细的部分：具体渠道、具体动作、可复制）
   - 可复制的：「在 Google Maps 上搜本地装修公司，用冷邮件发了 20 封，拿下 3 个客户」「在小红书发 10 篇笔记，第 3 篇爆了带来前 20 个咨询」
   - 不可复制的：「我靠一条爆款推文赚了 10 万美金」→ 偶然流量红利，不算真获客路径
   - 必须写清：在哪个平台/渠道、用什么具体内容或动作、第一波客户具体从哪来
   - 如果文章没有透露获客方法 → 在 practical_steps 写「未透露获客路径」，同时 authenticity 至少扣 2 分

3. **用到的工具链组合**
   - 列出该模式实际用到的无代码工具（如 Notion+Airtable+n8n+Stripe）
   - 如果工具链全是现成的、点鼠标就能用的 → 加分
   - 如果工具链需要自建/写代码 → 减分

要求：每条步骤具体到「在哪个平台做什么事」，不能泛泛说「做好内容就行」「多发多试」。

如果文章本身没有透露这些信息 → 在 practical_steps 里写「文章未提供足够实操信息」，同时 authenticity 至少扣 2 分。

---

## 四、结论判定

综合以上三个维度，给出判定：

- **「可复刻的真机会」**：交付物是服务/流程（不是软件/SaaS），工具链是现成的，核心瓶颈在获客而非技术。普通人可以照做。
- **「卖噱头/卖铲子」**：本质是"教人赚钱"而非"自己赚钱"，或者内容空洞无实操，或者需要编程能力。

---

## 三条一票否决红线（在 verdict 中体现）

以下三条任何一条命中 → 直接 irrelevant=false，verdict=卖噱头/卖铲子：

### 红线 1：交付物必须是"数字中介/自动化工作流服务"，不是"软件/SaaS"
- 要淘汰：「我开发了一个 AI SaaS 软件」（需要持续写代码维护、打补丁、数据库运维）
- 要选择：「我帮某中小企业用 Make + DeepSeek 搭建了自动把客户邮件转为 Notion 任务的系统，按月收服务费」（本质是无代码时代的自动化顾问）
- 记住：真正的无代码机会，技术壁垒几乎为零，真正的壁垒在于"发现了某个具体行业的烦人痛点"

### 红线 2：技术壁垒在"工具组合"而非"代码开发"
- 真实的非代码机会，工具链组合通常是：Notion/Airtable（数据）+ n8n/Make（连接）+ Claude/DeepSeek API（AI）+ Stripe/Gumroad（收款）
- 如果项目强调"技术难度极高"或"需要自行开发"→ 直接淘汰
- 如果项目的核心是"把几个现成工具拼起来解决一个行业痛点"→ 符合

### 红线 3：获客路径可复制，瓶颈在"获客"而非"技术"
- 要看：「如何通过冷邮件拿下前 3 个客户」「在哪个冷门论坛找到了目标用户」
- 不要看：「我靠一条爆款推文赚了 10 万美金」（偶然流量红利，不可复制）
- 不要看：「技术架构、系统设计、数据库选型」（技术瓶颈，普通人做不了）
- 这才是非技术人员应该复制的实操经验。

---

## 术语禁用与替换表
以下词汇绝对不要出现在 summary、opportunity_hint、practical_steps 中：
- SaaS → 「在线工具/软件」
- MRR → 「每月收入」
- SEO → 「让文章在搜索引擎排名靠前」
- Affiliate → 「推广别人的产品拿提成」
- niche → 「细分领域」
- 引流 → 「吸引顾客/吸引粉丝」
- Product Hunt → 「海外新品推荐网站」
- Gumroad → 「海外数字产品售卖平台」
- Notion → 「在线笔记工具」
- Python/代码/脚本/编程/开发/API → 「让 AI 帮你写」
- CAC/LTV/ROI/PMF/ARR 等缩写 → 一律用中文解释或不出现
- summary 里不要夹任何英文单词或缩写，全中文。

---

## 五、客观子因子打分（重要：你不要给"总分"，只回答事实）

**你没有权限给这条机会打总分。** 总分由程序用固定公式算，你只负责回答下面 11 个
「能从原文观察到的事实型问题」，每个打 1-5 分，档位定义如下。

{FACTOR_RUBRIC}

**纪律**：
- 原文写清楚了才给 4-5 分；原文没写、要靠你猜的，一律给 2-3 分。
- 不要因为"这个方向我觉得有前途"给高分，只看原文有没有证据。
- channel（获客路径清晰）是所有子项里最重要的一项，宁可给低不要给高。

---

## 六、可抄模板（照着做的最小行动包）

给每条真机会写一个「明天就能开工」的最小行动包，五个字段都要填，不许空泛：

- **who**：卖给谁（具体到人群+场景，如「开在小区里的宠物店老板」，不许写"中小企业"）
- **what**：卖什么（一句话说清交付物，如「一份每周更新的本地活动清单」）
- **first_step**：今天下班后 2 小时内能做完的第一步（具体到打开哪个网站、做什么动作）
- **first_prompt**：第一句可以直接复制粘贴给 AI 的话（完整一句，不是描述，是原话）
- **cost**：启动要花多少钱、多少时间（如「0 元，每天 1 小时，两周见第一单」）

---

## 输出格式

返回严格 JSON 数组，每条必须包含：

### 相关条目 (relevant=true):
```json
{
  "index": 0,
  "relevant": true,
  "translation": "中文翻译（英文必译，中文留空字符串）",
  "summary": "文章大意（80-120字）：大白话讲清楚这人做了什么、怎么做的、赚了多少。**必须先点出「他怎么获客的」——在哪个渠道、用什么具体动作拉来第一批客户**（这是读者最想知道的）；最后加💭我的判断：在中国能不能做、启动成本、最大风险。不许出现英文缩写。",
  "opportunity_hint": "普通人怎么模仿：卖什么 × 去哪卖 × 怎么找顾客（核心，必须具体：哪个平台/什么动作）× 收多少钱。30-40字，禁止术语。",
  "code_dependency": 2,
  "authenticity": 4,
  "practical_steps": "1. 交付物：xxx（什么东西、给谁用）\n2. 前5个客户：xxx（具体渠道、方法）\n3. 工具链：xxx（用的什么无代码工具组合）",
  "verdict": "可复刻的真机会",
  "difficulty": "零门槛",
  "quality_flag": "⭐",
  "factors": {
    "urgency": 4, "market_size": 3, "pricing": 4, "repeat": 3,
    "moat": 2, "margin": 4, "evergreen": 4,
    "channel": 4, "capital": 5, "speed": 4, "skill": 5
  },
  "copy_template": {
    "who": "开在小区里的宠物店老板",
    "what": "一份每月更新的宠物节日营销素材包",
    "first_step": "打开地图软件搜本市「宠物店」，抄下 30 家店的名字和电话，存进表格",
    "first_prompt": "帮我写一条 80 字以内的微信开场白，发给宠物店老板，介绍我可以每月给他做一套节日海报文案，第一次免费试用。语气自然、不要像推销。",
    "cost": "0 元，每天 1 小时，两周内谈下第一单"
  }
}
```

### 不相关条目 (relevant=false):
```json
{
  "index": 1,
  "relevant": false,
  "reason": "代码依赖度5分，纯编程内容，普通人无法复刻",
  "verdict": "卖噱头/卖铲子"
}
```

## 字段说明
- **code_dependency**: 整数 1-5，代码依赖度评分
- **authenticity**: 整数 1-5，真实性与水分评分
- **practical_steps**: 核心实操步骤三部分：1.交付物(什么东西给谁用) 2.前5个付费客户怎么来的(具体渠道方法) 3.工具链(用的无代码工具组合)。如果文章信息不足，写「文章未提供足够实操信息」
- **verdict**: 「可复刻的真机会」或「卖噱头/卖铲子」
- **difficulty**: 「零门槛」「需学习」「有一定门槛」
- **quality_flag**: 「⭐」（高价值信号：细节丰富、有社区验证、小渠道首发）、「」（正常）、「⚠️」（有风险信号：来源可疑、标题党、可能有水分）
- **factors**: 上面 11 个子因子，每个 1-5 的整数，一个都不能少（缺失会被当成 3 分处理，等于浪费这条机会）
- **copy_template**: 五个字段 who / what / first_step / first_prompt / cost，全部必填

## 严格要求
1. 纯 JSON 数组，不要 markdown 代码块包裹
2. 每条都要有 index
3. 英文必翻译成中文
4. 术语全部用中文大白话替换
5. 代码依赖度 >= 4 → relevant=false，verdict=卖噱头/卖铲子
6. 真实性 <= 2 → relevant=false，verdict=卖噱头/卖铲子
7. 三条一票否决红线命中任一条 → relevant=false，verdict=卖噱头/卖铲子
8. relevant=false 的条目只返回 index、relevant、reason、verdict
9. irrelevant 的也要返回，不要省略
10. 不要输出 relevance_score / 总分 / 星级评价——总分由程序计算，你输出了也会被忽略"""


BATCH_SYSTEM_PROMPT = BATCH_SYSTEM_PROMPT.replace("{FACTOR_RUBRIC}", FACTOR_RUBRIC)




class AIProcessor:
    """AI 内容处理器（批量模式）。"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 max_tokens: int = 8000, temperature: float = 0.2):
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
        """批量处理全部内容。

        按 token 预算自动分块：单次 API 调用的输出受 max_tokens 限制，若把所有候选
        塞进一次调用，响应会被截断、整批 JSON 解析失败（历史上导致「国内 0 条」）。
        改为拆成多块并发处理；某块若仍被截断/解析失败，自动「减半重试」，直到成功或
        拆到单条——保证不丢数据、也不因一次抖动拖垮全量。
        """
        if not self._enabled or not items:
            return items

        sem = asyncio.Semaphore(3)  # 并发上限，避免瞬时打爆 API

        async def _safe(chunk: list[ContentItem]):
            async with sem:
                return await self._process_chunk(chunk)

        work = list(self._chunk_items(items))
        if len(work) > 1:
            logger.info(
                f"内容过多（{len(items)} 条），按 token 预算拆分为 {len(work)} 批并发处理"
                f"（避免单次响应被截断导致整批丢失）"
            )

        out: list[ContentItem] = []
        # 失败自动减半重试，直到全部成功或拆到单条（单条仍失败则放弃该条）
        while work:
            outcomes = await asyncio.gather(*[_safe(c) for c in work])
            next_work: list[list[ContentItem]] = []
            for chunk, (processed, ok) in zip(work, outcomes):
                if ok:
                    out.extend(processed)
                elif len(chunk) > 1:
                    mid = len(chunk) // 2
                    next_work.append(chunk[:mid])
                    next_work.append(chunk[mid:])
                else:
                    # 单条仍失败（极端异常）→ 保留在结果里（ai_processed=False），顺序不缺
                    out.extend(processed)
            work = next_work
        return out

    @staticmethod
    def _est_input_tokens(item: ContentItem) -> int:
        """粗略估算单条输入占用的 token（中英文混合，偏保守：1 字符≈0.5 token）。"""
        title = item.title[:120]
        content = (item.full_text or item.summary or "(无内容)")[:1200]
        return (len(title) + len(content)) // 2 + 30

    def _chunk_items(
        self,
        items: list[ContentItem],
        max_input_tokens: int = 7000,
        per_item_output_est: int = 600,
        hard_cap: int = 14,
    ) -> list[list[ContentItem]]:
        """把内容按 token 预算切成多块，保证每块的输出不超 max_tokens、输入不超上下文。

        - per_item_output_est：单条完整 JSON 输出的估计 token 数（保守）。
        - budget_out = max_tokens * 0.8：给模型留思考/格式余量。
        - hard_cap：单块硬上限，防止个别超长内容把块撑爆。
        """
        budget_out = int(self.max_tokens * 0.8)
        chunks: list[list[ContentItem]] = []
        cur: list[ContentItem] = []
        in_tok = 0
        out_tok = 0
        for it in items:
            est = self._est_input_tokens(it)
            # 当前块非空且再加这条会超预算/硬上限 → 切块
            if cur and (
                len(cur) >= hard_cap
                or in_tok + est > max_input_tokens
                or out_tok + per_item_output_est > budget_out
            ):
                chunks.append(cur)
                cur = []
                in_tok = 0
                out_tok = 0
            cur.append(it)
            in_tok += est
            out_tok += per_item_output_est
        if cur:
            chunks.append(cur)
        return chunks

    async def _process_chunk(self, items: list[ContentItem]) -> tuple[list[ContentItem], bool]:
        """处理单块内容。返回 (被原地改写的同批条目, 是否成功)。

        失败 = API 调用失败 或 JSON 解析不出/被截断（条目数对不上）→ 上层会减半重试。
        """
        # 构建批量输入：优先使用全文，否则用摘要
        input_lines = []
        for i, item in enumerate(items):
            title = item.title[:120]
            # 有全文用全文（截取 1200 字给 AI），没全文用摘要
            content = (item.full_text or item.summary or "(无内容)")[:1200]
            input_lines.append(f"[{i}] {title} | {content}")
        user_content = "\n".join(input_lines)

        logger.info(f"批量处理 {len(items)} 条内容...")

        messages = [
            {"role": "system", "content": BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        content = await self._chat(messages, self.max_tokens, self.temperature)
        if content is None:
            # 重试后仍失败：标记本块所有条目为未处理，后续会被过滤
            logger.error("本批 AI 调用失败（重试后仍失败），该批内容不参与推送")
            for item in items:
                item.ai_processed = False
            return items, False

        # 解析 JSON
        results = self._parse_batch_response(content)
        if not results or len(results) < len(items):
            # 解析失败，或被截断（返回的条目数 < 输入条数）→ 上层减半重试
            logger.warning(
                "本批响应解析失败/被截断（%d/%d 条），将减半重试",
                len(results) if results else 0, len(items),
            )
            for item in items:
                item.ai_processed = False
            return items, False

        # 应用到原始条目（index 是本块内的局部下标，与 items 一一对应）
        result_map = {r["index"]: r for r in results if isinstance(r, dict)}
        for i, item in enumerate(items):
            r = result_map.get(i)
            if r and r.get("relevant"):
                item.translation = r.get("translation", "")
                item.ai_summary = r.get("summary", "")
                item.opportunity_hint = r.get("opportunity_hint", "")
                item.difficulty = r.get("difficulty", "")
                item.quality_flag = r.get("quality_flag", "")
                # 新评估维度
                if isinstance(r.get("code_dependency"), (int, float)):
                    item.code_dependency = int(r["code_dependency"])
                if isinstance(r.get("authenticity"), (int, float)):
                    item.authenticity = int(r["authenticity"])
                item.practical_steps = r.get("practical_steps", "")
                item.verdict = r.get("verdict", "")
                # 可抄模板
                tpl = r.get("copy_template")
                if isinstance(tpl, dict):
                    item.copy_template = {
                        k: str(v).strip()
                        for k, v in tpl.items()
                        if k in ("who", "what", "first_step", "first_prompt", "cost") and v
                    }
                # ⚠️ 总分不采信 AI 自评，一律由 scoring.py 的固定公式重算
                factors = r.get("factors")
                if not isinstance(factors, dict):
                    factors = {}
                    logger.warning(
                        "条目[%d] 缺少 factors 子因子，按保守中位数 3 分计算", i
                    )
                apply_to_item(item, factors)
                item.ai_processed = True
            else:
                # 标记为不相关，后续会被过滤
                item.relevance_score = 0.0
                item.verdict = r.get("verdict", "卖噱头/卖铲子") if r else "卖噱头/卖铲子"
                item.ai_processed = True

        return items, True

    # ============================================================
    # 通用 LLM 调用（供拆解引擎 / 发现引擎复用）
    # ============================================================

    async def _chat(self, messages: list[dict], max_tokens: int, temperature: float) -> Optional[str]:
        """带重试的对话调用，返回模型文本；全部失败后返回 None。"""
        last_err = ""
        for attempt in range(3):
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
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = str(e) or repr(e)
                if attempt < 2:
                    logger.warning(f"LLM 调用失败(第{attempt+1}次)，{2*(attempt+1)}s 后重试: {last_err[:120]}")
                    await asyncio.sleep(2 * (attempt + 1))
        logger.error(f"LLM 调用失败(已重试3次): {last_err[:200]}")
        return None

    async def call_llm(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """单次 LLM 调用，返回文本。未启用时返回空串。"""
        if not self._enabled:
            logger.warning("AI 未启用，跳过 LLM 调用")
            return ""
        content = await self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens,
            temperature,
        )
        return content if content is not None else ""

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
