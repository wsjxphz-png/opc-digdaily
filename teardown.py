"""
拆解引擎 (Teardown Engine)

把一名操盘手拆成「普通人能照着学」的商业拆解卡：
  - 人物画像 / 交付物 / 商业模式(怎么收费) / 获客方式 / 工具链
  - 可复制性(给非程序员打分 1-5) / 模仿第一步 / 是否卖铲子风险 / 值得学什么

数据来源（混合模式）：
  1. 该人 dossier 里的「种子事实」(seeded_facts) — 联网补搜得到的准确基线
  2. 近期抓到的该人内容信号 (signals) — 新鲜一手信息
  3. LLM 自身知识 — 填补空白
  → 任何不确定/未公开的具体数字，必须标注「未公开/约」，严禁编造。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from operators import Operator
from scoring import (
    compute,
    FACTOR_RUBRIC,
    gate_summary,
    operator_severity,
    AI_RATED_KEYS,
)
from filters import is_hype

logger = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))


def _clamp_factor(v) -> int:
    """把模型返回的 dbs 因子收敛成 1-5 整数；缺失/非法给保守中位数 3。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, n))


def _operator_authenticity(red_flag: str, learn: str) -> int:
    """从拆解卡的 red_flag / learn 判断是否「纯卖铲子 / 骗局」（命中则 dbs 判 skip）。

    口径与用户允许方向对齐（用户允许：内容 / 信息产品·课程·训练营 / 付费社群 / 产品化服务）：
      · 卖「真实技能」课（YouTube 增长、写作、剪辑、设计、英语…）属于允许的信息产品，不算卖铲子；
      · 只有「卖一人公司 / 副业 / 躺赚 / 暴富 等赚钱梦」的纯铲子党，或明显骗局，才判 authenticity<=2。
    判定（任一命中即 <=2，触发 skip）：
      1) 明确骗局词：庞氏 / 传销 / 拉人头 / 割韭菜 / 资金盘 / 金字塔 / 直销(传销式)
      2) red_flag 点明「卖铲子」且同时是「教一人公司 / 副业 / 财务自由 / 躺赚 / 搞钱 / 赚钱课 / 暴富」等赚钱梦语境

    死板教条修复：裸子串匹配会误杀「否定式澄清」——「没有明显的割韭菜行为」
    「并非传销或资金盘」「未发现拉人头等问题」本是该推的真实操盘手，却因为
    含骗局词被直接判 skip。修法：只有「未被否定修饰」的骗局词命中才触发，
    词前窗口出现「没有 / 并非 / 未发现 …」即视为在澄清、不触发。
    """
    text = f"{red_flag or ''} {learn or ''}"
    text_lower = text.lower()

    # 否定词窗口：出现这些词说明是在「澄清没有骗局」，该处命中不应触发 skip
    _NEG_CUES = ["没有", "无", "不是", "并非", "非", "未发现", "没", "不",
                 "不存在", "杜绝", "远离", "拒绝", "否认", "避免", "未见", "毫无"]

    def _unnegated_hit(word: str, window: int = 6) -> bool:
        """word 是否在文本中出现，且出现处之前 window 字符内没有否定词。"""
        idx = text_lower.find(word)
        while idx != -1:
            before = text_lower[max(0, idx - window):idx]
            if not any(c in before for c in _NEG_CUES):
                return True
            idx = text_lower.find(word, idx + 1)
        return False

    scam = ["庞氏", "传销", "拉人头", "割韭菜", "资金盘", "金字塔", "直销"]
    if any(_unnegated_hit(s) for s in scam):
        return 2
    if _unnegated_hit("卖铲子"):
        dream = ["一人公司", "做opc", "做OPC", "副业", "财务自由", "被动收入",
                 "躺赚", "搞钱", "赚钱课", "暴富", "月入过万", "月入十万"]
        if any(d in text_lower for d in dream):
            return 2
    return 3


def _tech_barrier_to_code_dep(tb: str) -> int:
    """技术门槛 → 代码依赖度 1-5（与 scoring.compute 口径一致）。"""
    return {"无": 1, "低": 2, "中": 3, "高": 4}.get((tb or "").strip(), 3)


DBS_PROMPT_SUFFIX = (
    "\n\n## 商业底层逻辑体检（dontbesilent 商业本体论 16 因子）\n"
    + FACTOR_RUBRIC
    + "\n\n请在输出 JSON 中补充一个 `dbs` 对象，逐项填上面 16 个因子的 1-5 分"
    "（no_code 已由技术门槛反算，不在此列；只打分，不解释）：\n"
    "urgency / pricing / margin / repeat / price_ladder / revenue_proof / "
    "market_size / evergreen / channel / machine / delivery_chain / replicable / "
    "capital / speed / skill / concrete"
)


TEARDOWN_SYSTEM_PROMPT = """你是一名「商业拆解教练」，专门把一个赚钱的超级个体 / 单人数字化工作室，拆成完全不会写代码的人也能照着学的结构。

## 你的用户画像
他完全不会写代码，不知道什么叫编程。他只负责：看案例 → 理解别人怎么赚钱 → 照着模仿。你输出的每一句，都要让他能直接抄作业。

## 你要拆解的对象
不是「一篇文章」，而是一个「人」——他靠什么交付、怎么收费、客户从哪来。你要还原他的生意全貌。

## 四条铁律
1. **不编造数字**：定价、收入、客户数等具体数字，如果资料里没有，就写「未公开」；只有从资料能合理推断时才写「约 X」并说明是推断。绝不允许凭空编 MRR / 客单价。
2. **获客是重中之重**：读者最想知道的不是「这人赚了多少」，而是「客户到底是怎么找上门的」。所以整张卡里「获客方式」必须讲得最细、最具体——在哪个平台/渠道、用什么内容或动作（冷邮件/发帖/地推/社群/合作/SEO 等）、第一波客户从哪来、有没有可照抄的具体打法。少写个人故事和鸡汤，多写「客户从哪来」。
3. **可复制性优先**：如果一个模式普通人照做门槛极高（要写代码、要融资、要团队），明确说出来，并在「模仿第一步」给一个降级版（不写代码的替代路径）。
4. **识别卖铲子**：如果他主要收入来自「教别人做一人公司 / 卖课 / 卖社群 / 卖模板」，而不是自己交付真实服务或产品 → 在 red_flag 里点明，replicability 压低。
5. **技术门槛明说**：在 tech_barrier 字段明确标出这个模式对「完全不会写代码的人」的技术要求——「无」=完全不用碰代码/软件；「低」=可用 ChatGPT/Notion/表单等现成无代码工具；「中」=需要配置较复杂工具或有一点技术；「高」=必须自己写代码/开发软件。在 doable 字段用一句话直说「这个人你能否照做」：能 / 降级可做（用无代码工具替代）/ 做不到（需写代码）。凡是核心交付物是软件的（SaaS、代码模板、自动化工作流），tech_barrier 必须标「高」、doable 必须标「做不到」。

## 术语禁用（面向非程序员）
SaaS→「在线工具/软件」；MRR→「每月收入」；SEO→「让内容在搜索引擎好找」；API→「让 AI 帮你接」；niche→「细分领域」。禁止出现英文缩写。

## 全中文、能翻就翻
读者看不懂英文。整张卡**除账号/handle 等检索标识外，一律用中文**：
- 源材料里的英文人名、公司名、产品名、平台名、术语，能翻译成中文的就翻成中文；
- 没有通用中文译名的专有名词（如某个具体海外工具）才保留英文，并在首次出现时用中文括注解释，例如「Substack（海外付费邮件订阅平台）」「Gumroad（海外数字产品售卖平台）」；
- 标题、字段值、正文都不要夹英文单词或缩写。

## 输出格式（严格 JSON，不要 markdown 包裹）
{
  "who": "人物画像：他是谁、大致做什么生意、规模/阶段（如：单人、月入级别若可知）",
  "deliverable": "交付物：他到底卖什么/交付什么——是服务、产品、资产还是流程？给谁用？",
  "business_model": "商业模式：怎么收费（一次性/项目制/月费/订阅/广告）、大致价格区间（有就写，没有写未公开）、收入结构",
  "acquisition": "获客方式（这是整张卡最该讲透的部分，字数可以最多）：他到底靠什么具体动作把陌生人变成客户？必须写清——①在哪个平台/渠道（具体名字，如小红书/ Reddit 某板块/ 本地微信群）；②用什么具体内容或动作（冷邮件模板、爆款笔记、地推话术、免费资源引流等）；③第一波客户从哪来（前 3-5 个客户的具体来源）；④有没有可照抄的打法（普通人今天就能模仿的一步）。严禁泛泛写「靠内容引流」「多发多试」「做好私域」这类空话。资料真没透露获客路径，才写「未透露」并说明这降低了可参考性。",
  "stack": "工具链：他用哪些现成无代码/AI工具（Notion/n8n/Make/Stripe/Gumroad 等），点鼠标就能用的加分",
  "replicability": 4,
  "first_step": "模仿第一步：一个完全不会写代码的人，今天就能做的第一件事（具体、可执行）",
  "red_flag": "风险/卖铲子判断：他是不是主要靠教别人赚钱？普通人模仿的最大坑是什么？",
  "learn": "最该抄的作业：这个案例里最值得普通人学习的 1-2 个点",
  "tech_barrier": "无",
  "doable": "能（完全不用写代码，用现成工具即可）",
  "dbs": {
    "urgency": 3, "pricing": 3, "margin": 3, "repeat": 3, "price_ladder": 3,
    "revenue_proof": 3, "market_size": 3, "evergreen": 3,
    "channel": 3, "machine": 3, "delivery_chain": 3, "replicable": 3,
    "capital": 3, "speed": 3, "skill": 3, "concrete": 3
  }
}

## 字段说明
- replicability: 整数 1-5，给「完全不会写代码的人」的复制难度反向分（5=极易照做，1=基本做不到）
- tech_barrier: 字符串「无」/「低」/「中」/「高」，含义见铁律第 5 条
- doable: 给非程序员的一句话结论：「能」/「降级可做（用无代码工具替代）」/「做不到（需写代码）」
- 其他字段均为中文大白话字符串
- 严格只返回 JSON 对象，不要额外文字。字符串值内的引用一律用中文引号「」或“”，严禁在 JSON 字符串值里使用未转义的双引号，字段值不要带尾随逗号。"""
TEARDOWN_SYSTEM_PROMPT = TEARDOWN_SYSTEM_PROMPT + DBS_PROMPT_SUFFIX


REVISIT_SYSTEM_PROMPT = """你是一名「商业拆解教练」，现在要对一位**之前已经拆解过**的操盘手做「动态更新补充」。读者已经看过他上一次的拆解卡，他只关心一件事：**这之后，他有没有搞出新的业务、拓展了新的边界、出现了新的赚钱方式？**

## 你的任务
基于「上一次拆解的摘要」+「这段时间里抓到的新内容信号」，只输出**新增 / 变化**的部分：
- 新交付物 / 新产品 / 新服务
- 新商业模式 / 新收费方式
- 新获客渠道 / 新打法
- 新边界拓展（从一人到小团队？从国内到出海？从内容到实体？）
如果这段时间**没有实质新动态**，就明确写「暂无重大新动态」，不要硬编。

## 五条铁律（同拆解卡）
1. **不编造数字**：定价、收入等资料里没有就写「未公开」，严禁凭空编。
2. **获客是重中之重**：新获客动作要讲最细（哪个平台/渠道、什么具体内容或动作、第一波客户从哪来）。
3. **可复制性优先**：普通人照做门槛极高时，给降级路径（不写代码的替代）。
4. **识别卖铲子**：如果他开始主要靠「教别人」赚钱，点明，replicability 压低。
5. **技术门槛明说**（无/低/中/高）；doable：能 / 降级可做 / 做不到。凡是核心交付物是软件的，tech_barrier 必须标「高」、doable 必须标「做不到」。

## 术语禁用 + 全中文（同拆解卡）
SaaS→「在线工具/软件」；MRR→「每月收入」；SEO→「让内容在搜索引擎好找」；API→「让 AI 帮你接」；niche→「细分领域」。整张卡除账号/handle 外一律用中文。

## 输出格式（严格 JSON，不要 markdown 包裹）
{
  "who": "人物画像（可沿用上次，仅补充新变化）",
  "deliverable": "交付物（重点写「新增/变化」的部分）",
  "business_model": "商业模式（重点写新收费/新业务）",
  "acquisition": "获客方式（重点写新渠道/新打法，最细）",
  "stack": "工具链（有变化才写）",
  "replicability": 4,
  "first_step": "普通人今天就能抄的一步（基于他的新动态）",
  "red_flag": "风险/卖铲子判断（这次有没有新坑）",
  "learn": "最该抄的新作业（这次动态里最值得学的 1-2 点）",
  "tech_barrier": "无",
  "doable": "能（完全不用写代码，用现成工具即可）",
  "dbs": {
    "urgency": 3, "pricing": 3, "margin": 3, "repeat": 3, "price_ladder": 3,
    "revenue_proof": 3, "market_size": 3, "evergreen": 3,
    "channel": 3, "machine": 3, "delivery_chain": 3, "replicable": 3,
    "capital": 3, "speed": 3, "skill": 3, "concrete": 3
  }
}
## 字段说明
- replicability: 整数 1-5，给「完全不会写代码的人」的复制难度反向分（5=极易照做，1=基本做不到）
- tech_barrier: 字符串「无」/「低」/「中」/「高」
- doable: 「能」/「降级可做（用无代码工具替代）」/「做不到（需写代码）」
- 严格只返回 JSON 对象，不要额外文字。字符串值内的引用一律用中文引号「」或“”，严禁在 JSON 字符串值里使用未转义的双引号，字段值不要带尾随逗号。"""
REVISIT_SYSTEM_PROMPT = REVISIT_SYSTEM_PROMPT + DBS_PROMPT_SUFFIX


@dataclass
class Teardown:
    operator_handle: str
    operator_name: str
    region: str
    who: str = ""
    deliverable: str = ""
    business_model: str = ""
    acquisition: str = ""
    stack: str = ""
    replicability: int = 0
    first_step: str = ""
    red_flag: str = ""
    learn: str = ""
    tech_barrier: str = ""
    doable: str = ""
    signals_used: int = 0
    generated_at: str = ""
    is_revisit: bool = False   # True=这是一份「动态更新补充」卡（基于上次拆解后的新动态）
    commercial_health: dict = None  # dbs 商业底层逻辑体检结果（compute 返回的原始 dict + gate_reason）

    def to_dict(self) -> dict:
        return {
            "operator_handle": self.operator_handle,
            "operator_name": self.operator_name,
            "region": self.region,
            "who": self.who,
            "deliverable": self.deliverable,
            "business_model": self.business_model,
            "acquisition": self.acquisition,
            "stack": self.stack,
            "replicability": self.replicability,
            "first_step": self.first_step,
            "red_flag": self.red_flag,
            "learn": self.learn,
            "tech_barrier": self.tech_barrier,
            "doable": self.doable,
            "signals_used": self.signals_used,
            "generated_at": self.generated_at,
            "is_revisit": self.is_revisit,
            "commercial_health": self.commercial_health,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Teardown":
        return cls(
            operator_handle=d.get("operator_handle", ""),
            operator_name=d.get("operator_name", ""),
            region=d.get("region", ""),
            who=d.get("who", ""),
            deliverable=d.get("deliverable", ""),
            business_model=d.get("business_model", ""),
            acquisition=d.get("acquisition", ""),
            stack=d.get("stack", ""),
            replicability=d.get("replicability", 0) or 0,
            first_step=d.get("first_step", ""),
            red_flag=d.get("red_flag", ""),
            learn=d.get("learn", ""),
            tech_barrier=d.get("tech_barrier", ""),
            doable=d.get("doable", ""),
            signals_used=d.get("signals_used", 0) or 0,
            generated_at=d.get("generated_at", ""),
            is_revisit=bool(d.get("is_revisit", False)),
            commercial_health=d.get("commercial_health"),
        )


class TeardownEngine:
    """根据操盘手 dossier 合成拆解卡。"""

    def __init__(self, ai):
        self.ai = ai

    def _build_user_content(self, op: Operator) -> str:
        lines = []
        lines.append(f"# 拆解对象：{op.name}（{'国内' if op.region == '国内' else '国际'}）")
        if op.category:
            lines.append(f"已知标签：{op.category}")

        # 种子事实（联网补搜的准确基线）
        if op.seeded_facts:
            lines.append("\n## 已核实事实（来自公开资料，优先采用）")
            fact_labels = {
                "deliverable": "交付物",
                "business_model": "商业模式/收费",
                "acquisition": "获客方式",
                "stack": "工具链",
                "mrr": "收入/规模",
                "first_step": "模仿路径",
            }
            for k, label in fact_labels.items():
                v = op.seeded_facts.get(k)
                if v:
                    lines.append(f"- {label}：{v}")

        # 近期信号（一手新鲜内容）
        if op.signals:
            lines.append(
                f"\n## 近期抓到的该人内容（{len(op.signals)} 条，作为新鲜信号）"
            )
            for i, s in enumerate(op.signals[-8:], 1):
                snippet = (s.get("summary") or "")[:200]
                lines.append(f"{i}. {s.get('title', '')} | {snippet}")

        if not op.seeded_facts and not op.signals:
            lines.append(
                "\n（暂无抓取信号与核实事实，请基于你的知识尽可能还原其商业逻辑，"
                "不确定处标注「未公开」）"
            )

        lines.append(
            "\n请按系统指令输出该操盘手的结构化商业拆解 JSON。"
        )
        return "\n".join(lines)

    def _score_business_logic(self, op: Operator, data: dict, td: "Teardown") -> str:
        """对同一道 LLM 调用返回的 dbs 因子打分，跑 dbs 商业本体论体检，
        把结果写回 td 与 op（commercial_health + commercial_severity）。返回分档。
        """
        dbs = data.get("dbs") or {}
        factors = {k: _clamp_factor(dbs.get(k)) for k in AI_RATED_KEYS}
        code_dependency = _tech_barrier_to_code_dep(td.tech_barrier)
        authenticity = _operator_authenticity(td.red_flag, td.learn)
        hype = is_hype(
            " ".join([
                td.who, td.deliverable, td.business_model,
                td.acquisition, td.red_flag, td.learn,
            ])
        )
        res = compute(
            factors,
            code_dependency=code_dependency,
            authenticity=authenticity,
            hype=hype,
        )
        res["gate_reason"] = gate_summary(res)
        severity = operator_severity(res, authenticity=authenticity, hype=hype)
        td.commercial_health = res
        op.commercial_health = res
        op.commercial_severity = severity
        logger.info(
            f"[{op.name}] dbs 商业体检：商业化 {res['commercial']}/100 · "
            f"可行性 {res['feasibility']}/100 · 适合启动 {res['startup_index']}/10 · "
            f"分档={severity}"
        )
        return severity

    async def synthesize(self, op: Operator) -> Optional[Teardown]:
        """合成一名操盘手的拆解卡，并写回其 dossier。"""
        user_content = self._build_user_content(op)
        raw = await self.ai.call_llm(
            TEARDOWN_SYSTEM_PROMPT, user_content, max_tokens=2000, temperature=0.3
        )
        if not raw:
            logger.warning(f"[{op.name}] LLM 无返回，跳过拆解")
            return None

        data = self._parse(raw)
        if not data:
            return None

        # 只取 schema 内的已知字段，忽略模型多返回的杂项键，
        # 否则 Teardown.from_dict(**data) 会因未知关键字直接 TypeError 崩溃。
        KNOWN = {
            "who", "deliverable", "business_model", "acquisition",
            "stack", "replicability", "first_step", "red_flag", "learn",
            "tech_barrier", "doable",
        }
        clean = {k: data[k] for k in KNOWN if k in data}
        rep = data.get("replicability")
        try:
            clean["replicability"] = int(rep) if rep is not None else 0
        except (TypeError, ValueError):
            clean["replicability"] = 0
        # 技术门槛归一化：只接受 无/低/中/高
        tb = (clean.get("tech_barrier") or "").strip()
        if tb not in ("无", "低", "中", "高"):
            clean["tech_barrier"] = ""

        td = Teardown.from_dict({
            "operator_handle": op.handle,
            "operator_name": op.name,
            "region": op.region,
            **clean,
            "signals_used": len(op.signals),
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        })

        # dbs 商业底层逻辑体检（复用同一道 LLM 返回的 dbs 因子，免费算）
        self._score_business_logic(op, data, td)

        # 写回 dossier
        op.teardown = td.to_dict()
        op.last_teardown = datetime.now(CST).strftime("%Y-%m-%d")
        op.teardown_count += 1
        logger.info(f"[{op.name}] 拆解完成 (可复制性 {td.replicability}/5)")
        return td

    def _build_revisit_user_content(self, op: Operator, prev: dict | None) -> str:
        """组装「动态更新」的用户内容：上次拆解摘要 + 这段时间的全新信号。"""
        lines = []
        lines.append(f"# 动态更新对象：{op.name}（{'国内' if op.region == '国内' else '国际'}）")
        if prev:
            lines.append("\n## 上一次拆解摘要（读者已看过，只补新东西）")
            for k, label in [
                ("who", "人物"), ("deliverable", "交付物"),
                ("business_model", "商业模式"), ("acquisition", "获客方式"),
            ]:
                v = prev.get(k)
                if v:
                    lines.append(f"- {label}：{v}")
        if op.signals:
            lines.append(
                f"\n## 这段时间抓到的新内容信号（{len(op.signals)} 条，重点看这里）"
            )
            for i, s in enumerate(op.signals[-10:], 1):
                snippet = (s.get("summary") or "")[:250]
                lines.append(f"{i}. [{s.get('date', '')}] {s.get('title', '')} | {snippet}")
        else:
            lines.append(
                "\n（暂无明显新信号，请基于已知判断是否值得补充；无新动态就明确写「暂无重大新动态」）"
            )
        lines.append("\n请按系统指令输出该操盘手的「动态更新补充」JSON。")
        return "\n".join(lines)

    async def synthesize_revisit(
        self, op: Operator, prev: dict | None
    ) -> Optional[Teardown]:
        """对已拆解操盘手做「动态更新补充」，聚焦他自上次拆解后的新业务/新边界/新赚钱方式。"""
        user_content = self._build_revisit_user_content(op, prev)
        raw = await self.ai.call_llm(
            REVISIT_SYSTEM_PROMPT, user_content, max_tokens=2000, temperature=0.3
        )
        if not raw:
            logger.warning(f"[{op.name}] 复盘更新 LLM 无返回，跳过")
            return None

        data = self._parse(raw)
        if not data:
            return None

        KNOWN = {
            "who", "deliverable", "business_model", "acquisition",
            "stack", "replicability", "first_step", "red_flag", "learn",
            "tech_barrier", "doable",
        }
        clean = {k: data[k] for k in KNOWN if k in data}
        rep = data.get("replicability")
        try:
            clean["replicability"] = int(rep) if rep is not None else 0
        except (TypeError, ValueError):
            clean["replicability"] = 0
        tb = (clean.get("tech_barrier") or "").strip()
        if tb not in ("无", "低", "中", "高"):
            clean["tech_barrier"] = ""

        td = Teardown.from_dict({
            "operator_handle": op.handle,
            "operator_name": op.name,
            "region": op.region,
            "is_revisit": True,
            **clean,
            "signals_used": len(op.signals),
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        })

        # dbs 商业底层逻辑体检（复用同一道 LLM 返回的 dbs 因子，免费算）
        self._score_business_logic(op, data, td)

        # 写回 dossier（更新为最新拆解，含新动态）
        op.teardown = td.to_dict()
        op.last_revisit = datetime.now(CST).strftime("%Y-%m-%d")
        op.revisit_count += 1
        logger.info(f"[{op.name}] 动态更新补充完成 (第 {op.revisit_count} 次)")
        return td

    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        import jsonfix

        obj = jsonfix.parse_llm_json(raw, fallback_to_list=False)
        # 模型有时会返回数组（如 [{...}] 或 [{...},{...}]），取第一个对象
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        return obj if isinstance(obj, dict) else None
