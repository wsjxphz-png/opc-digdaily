"""
发现引擎 (Discovery Engine)

扫描抓到的内容，找出「新操盘手」——真正在赚钱的超级个体 / 单人工作室，
而不是媒体账号、不是教人创业的博主、不是卖课党。

这些新发现会被加入操盘手名单，后续进入「拆解循环」。
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from operators import OperatorRoster
from filters import is_technical

logger = logging.getLogger(__name__)


DISCOVERY_SYSTEM_PROMPT = """你是一个「新机会猎手」，专门在内容里挖掘「值得普通人学习的赚钱个人」。你的第一原则：**中国市场每天都有新的自媒体、新 IP、新的一人公司冒出来，永远有人值得推荐**——所以你 NEVER 以"没有合适的人"收场，而是尽全力从内容里找出最值得学的那一个/几个。

## 你的核心任务：从「长期价值」和「底层逻辑」切入，找值得学的人
不要只看名气、粉丝量、体量。一个人哪怕粉丝很小、影响力不大，只要他身上有「可复制的赚钱逻辑」或「值得借鉴的底层认知」，就值得被推荐。你要回答的是：
- 这个人的「赚钱底层逻辑」是什么？（他靠什么稀缺认知 / 交付 / 获客打法跑通？）
- 他的模式有没有「长期价值」？（不是蹭风口割韭菜，而是可持续、普通人能借鉴）
- 普通人能从他身上「抄到什么」？

## 你找的人（三类都算，体量不限）
1. **新的一人公司**：一个人刚跑通一门小生意（刚起步、还没成名也完全可以）
2. **新的 IP / 新自媒体**：刚做起来、还在上升期的新博主 / 新个人品牌（粉丝少没关系，只要有真东西）
3. **靠一个人模式赚到钱的人**：靠自己交付而非团队/融资，刚刚跑通闭环（哪怕规模很小）

注意：本系统的读者完全不会写代码。所以你只找「不需要写代码、不需要开发软件」就能赚钱的超级个体：
- 靠内容/自媒体/品牌合作赚钱（写文章、做短视频、运营社媒，靠广告/品牌/会员变现）
- 靠信息产品赚钱（卖电子书、线上课、训练营、知识库）
- 靠付费社群/会员/陪伴赚钱（建收费社群，做策展和陪伴，收会员费）
- 靠产品化服务赚钱（非技术：文案、获客外包、社媒代运营、虚拟助理、调研、排版等固定价格服务）
- 靠策展/联盟分销/中介撮合赚钱（整理别人的内容、推广别人的产品、撮合供需）
- 有真实交付过程和获客动作，不主要靠「教别人怎么赚钱」活着

## 什么不是（排除）
- 媒体/聚合账号：Starter Story、Indie Hackers、Product Hunt、各类「XX日报/资讯」号
- 教人创业/卖课/卖社群/卖模板的人（收入主要来自「教」，不是「做」）——除非他本人先靠真实交付跑通、卖课只是顺带
- 纯泛科技资讯、新闻、融资报道
- 大公司/需要团队或融资的事
- **写代码/做 SaaS / 卖代码模板 / 搞自动化工作流（n8n、Make、Zapier）/ 开发 AI 工具的人**——核心交付物是软件，普通人学不会，一律排除（tech_barrier 标「高」、is_operator 设 false）
- **技术大牛 / 技术博主 / 程序员出身、靠技术影响力（而非一门真实小生意）变现的人**——读者完全不懂代码，这类案例看了也学不会，一律排除（tech_barrier 标「高」、is_operator 设 false）
- **已经红了很久的成名大V / 头部博主 / 知名作家 / 老牌个人 IP**（如半佛仙人、Ali Abdaal、Pat Flynn 这类已经成名多年的）——他们不是「新机会」，读者已错过窗口期，不要返回（established 设 true、is_operator 设 false）

## 判断要点
- 内容是否在讲「某个人具体怎么做成了一门小生意 / 一个 IP」？→ 是猎物
- 这个人的「底层赚钱逻辑」是否清晰、是否值得普通人借鉴？→ 是猎物（体量大小不重要）
- 他是不是「刚冒头 / 刚起步 / 还在成长」？→ 优先（established 设 false）
- 如果内容只是泛泛方法论或新闻 → 不是

## 输出格式（严格 JSON 数组，不要 markdown 包裹）
对每条内容，返回：
{
  "index": 0,
  "is_operator": true,
  "name": "主角中文名/中文称呼（若有常用中文译名就用译名；没有则用中文音译或中文描述其身份，不要写英文）",
  "handle": "原始检索标识（Twitter @handle / 网站域名 / 真名；保留英文原文，用于检索，不要翻译）",
  "region": "国内" 或 "国际",
  "tech_barrier": "无" 或 "低",
  "established": false,
  "highlight": "一句话：为什么这个人值得推荐（他的赚钱底层逻辑/长期价值是什么，且无需写代码；哪怕粉丝很小也值得学）。用中文写。",
  "reason": "判断依据（从内容里哪句话看出来的；为何判断他值得学、且是新兴而非成名大V）。用中文写。"
}
只把 is_operator=true 的返回出来；如果某条没有合适的人，返回 is_operator=false（或省略）。
- established 字段：此人是否已经成名多年（true=已成名大V，不算新机会，应排除）；刚起步/刚被发现/粉丝尚小的新人填 false。"""


@dataclass
class DiscoveredOperator:
    name: str
    handle: str
    region: str
    highlight: str
    reason: str
    tech_barrier: str = "无"
    established: bool = False
    source_title: str = ""

    async def commit(self, roster: OperatorRoster):
        """把发现写入名单（按 handle/name 去重）。返回是否新加入。"""
        key = self.handle or self.name
        if not key:
            return False
        # 非技术才入名单：中/高（需写代码）直接丢弃
        if self.tech_barrier not in ("无", "低"):
            logger.info(f"发现 [{self.name}] 技术门槛={self.tech_barrier}，已排除（需写代码）")
            return False
        # 双保险：AI 万一误判（把技术大牛标成「无」），用关键词硬过滤兜底
        probe = f"{self.name} {self.handle} {self.highlight} {self.reason}"
        if is_technical(probe):
            logger.info(f"发现 [{self.name}] 命中技术关键词，已排除（不推技术向案例）")
            return False
        # 已成名多年的大V不是「新机会」，直接丢弃（读者要的是刚冒头的人）
        if self.established:
            logger.info(f"发现 [{self.name}] 已成名多年(established)，已排除（不是新机会）")
            return False
        # 已经在名单里（按 handle 或 name 模糊匹配）则跳过
        for op in roster.operators.values():
            if op.handle == key or op.name == self.name:
                return False
        region = self.region if self.region in ("国内", "国际") else "国际"
        aliases = [key] if key.startswith("@") else [self.name]
        added = roster.add_operator(
            handle=key,
            name=self.name,
            region=region,
            aliases=aliases,
            sources=["discovery"],
            highlight=self.highlight,
            tech_barrier=self.tech_barrier,
        )
        return added


class DiscoveryEngine:
    """从内容里发现新操盘手。"""

    def __init__(self, ai, max_scan: int = 30):
        self.ai = ai
        self.max_scan = max_scan

    async def scan(self, items: list, roster: OperatorRoster) -> list[DiscoveredOperator]:
        """扫描内容，返回发现的新操盘手（尚未写入名单）。"""
        # 只扫描「不在已知操盘手名下」的内容（媒体/Reddit/未知源）
        candidates = []
        for it in items:
            sn = getattr(it, "source_name", "") or ""
            matched = any(sn in op.aliases for op in roster.operators.values())
            if matched:
                continue
            text = f"{it.title}\n{(it.full_text or it.summary)[:300]}"
            if len(text.strip()) < 20:
                continue
            candidates.append((it, text))
            if len(candidates) >= self.max_scan:
                break

        if not candidates:
            return []

        logger.info(f"发现引擎扫描 {len(candidates)} 条候选内容...")
        input_lines = []
        for i, (it, text) in enumerate(candidates):
            input_lines.append(f"[{i}] {text}")
        user_content = "\n\n".join(input_lines)

        raw = await self.ai.call_llm(
            DISCOVERY_SYSTEM_PROMPT, user_content, max_tokens=3000, temperature=0.2
        )
        if not raw:
            return []

        results = self._parse(raw)
        discovered = []
        for r in results:
            if not r.get("is_operator"):
                continue
            name = (r.get("name") or "").strip()
            if not name:
                continue
            discovered.append(DiscoveredOperator(
                name=name,
                handle=(r.get("handle") or "").strip(),
                region=r.get("region", "国际"),
                highlight=(r.get("highlight") or "").strip(),
                reason=(r.get("reason") or "").strip(),
                tech_barrier=(r.get("tech_barrier") or "无").strip(),
                established=bool(r.get("established", False)),
            ))
        logger.info(f"发现引擎产出 {len(discovered)} 个候选操盘手")
        return discovered

    @staticmethod
    def _parse(raw: str) -> list[dict]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()

        def _as_list(obj):
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                return [obj]  # 模型有时直接返回单个对象而非数组
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, (list, dict)):
            return _as_list(data)

        # 尝试从文本提取数组
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return _as_list(json.loads(m.group()))
            except json.JSONDecodeError:
                pass
        # 尝试提取单个对象
        m2 = re.search(r"\{.*\}", raw, re.DOTALL)
        if m2:
            try:
                return _as_list(json.loads(m2.group()))
            except json.JSONDecodeError:
                pass
        logger.error(f"发现结果解析失败: {raw[:300]}")
        return []
