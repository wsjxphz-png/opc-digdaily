"""
客观商业打分引擎 (Objective Business Scoring) —— v2「dbs 商业本体论」版

设计原则 —— 「AI 只做观察，公式做判断」：
  ❌ 旧做法：让 AI 直接说「这条值 0.85 分」→ 主观、不稳定、无法解释、不可比较。
  ✅ 新做法：让 AI 只回答一批「可从原文观察到的事实型子问题」（每个 1-5 分，
     每档都有明确锚点定义），分数怎么加权、怎么封顶，全部写死在下面的 Python 公式里。
     同一篇文章，今天跑和明天跑得到同样的分；不同文章之间可以横向比较。

打分体系来源（公开可查的成熟框架，非自创）：
  1. **dontbesilent 商业本体论（dbs 工具箱，本机 skill）** —— 本版新增，也是权重的主心骨。
     六条公理 + 体检七项检验 + 对标五重过滤。它与前两套框架最大的不同是：
     **它是「否决式」的，不是「加权式」的**。很多在通用商业框架里算加分的东西
     （壁垒高、市场大、粉丝多），在这套框架里要么不算分，要么直接是减分项。
  2. Josh Kaufman《The Personal MBA》「十项经济性评估」：紧迫度 / 市场规模 / 定价潜力 /
     获客成本 / 交付成本 / 独特性 / 上市速度 / 前期投入 / 追加销售潜力 / 长青度。
  3. 独立创业圈的 BB Score（10 因子加权 0-100）。

dbs 框架带来的三处关键修正（都推翻了 v1 的做法）：
  ① **「竞争壁垒」不再是加分项，反转成「可复刻性」。**
     v1 给「别人抄不走」打 5 分。但本项目用户的诉求就是「抄」——
     别人抄不走，等于他也抄不走。同一个事实，对读者是负资产。
     dbs 信条 4：高毛利/高复购/高壁垒/高增长/高流量……全部 ≠ 高利润，我们只要高利润。
  ② **「流量」不再等于「赚钱」。** dbs 公理 4：99% 的情况下流量越大越不赚钱。
     只有粉丝数、播放量而拿不出收入证据的案例，商业化分要打折并封顶。
  ③ **新增「印钞机检验」（换个人喂同样的料，出不出同样的货）。**
     dbs 检验 1 + 检验 6：不能用员工代替老板的，不是生意，是高薪打工。
     对一人公司读者来说：必须靠原作者个人魅力/独家关系才成立的案例，学不会。

输出三个数字：
  · commercial_score  商业化潜力 0-100  ——「这门生意本身值不值钱」
  · feasibility_score 可行性     0-100  ——「你这个不会写代码的人能不能落地」
  · startup_index     适合你启动 1-10   —— 综合排序用，一眼看出今天最该抄哪条
"""

from __future__ import annotations

from typing import Any

# ============================================================
# 因子定义：(字段名, 中文名, 权重)
# 权重之和 = 1.0，每个因子 AI 打 1-5 分
# ============================================================

# ---- 商业化潜力：这门生意本身值不值钱（不考虑你会不会做）----
# 注意权重分配已按 dbs 信条 4「高利润是唯一标准」调整：
#   毛利/定价/复购（决定利润）> 市场规模（dbs 明确不以规模为标准）
COMMERCIAL_FACTORS: list[tuple[str, str, float]] = [
    ("urgency",       "需求紧迫度", 0.16),  # 客户是"现在就疼"还是"有了更好"
    ("pricing",       "单笔收费",   0.15),  # 一单能收多少钱
    ("margin",        "交付毛利",   0.14),  # 每单要搭进去多少时间和成本（dbs：只要高利润）
    ("repeat",        "持续付费",   0.13),  # 复购让获客成本趋近于 0
    ("price_ladder",  "定价结构",   0.12),  # dbs 公理5「定价即产品」：引流款/利润款价差
    ("revenue_proof", "收入证据",   0.12),  # dbs 公理4「流量≠收入」：真金白银 vs 只有粉丝数
    ("market_size",   "客户规模",   0.10),  # 降权：dbs 认为市场大小不是筛选标准
    ("evergreen",     "抗过时",     0.08),  # 明年这个需求还在不在
]

# ---- 可行性：你（不会写代码的人）能不能真的落地 ----
FEASIBILITY_FACTORS: list[tuple[str, str, float]] = [
    ("no_code",        "不用写代码",   0.20),  # 由 code_dependency 反算，AI 不单独打
    ("channel",        "获客路径清晰", 0.20),  # dbs 五重过滤筛子2 的第一段，最重要
    ("machine",        "换人也能做",   0.16),  # dbs 检验1 印钞机 + 检验6 规模化
    ("delivery_chain", "交付链路清晰", 0.12),  # dbs 筛子2 后三段：转化→交付→复购
    ("replicable",     "可复刻性",     0.10),  # dbs 信条3 模仿颗粒度（原 moat 反转）
    ("capital",        "启动资金低",   0.08),  # 0 成本起步 = 5
    ("speed",          "出单速度快",   0.08),  # 一周内能试出水花 vs 要熬几个月
    ("skill",          "技能门槛低",   0.06),  # 写字/拍照/沟通 vs 需要执照或专业资质
]

# ---- 独立闸门因子：不参与加权，只用于否决 ----
# dbs「产品颜色测试」：说不出产品是什么颜色的，就还没进入市场。
GATE_FACTORS: list[tuple[str, str]] = [
    ("concrete", "具体性"),
]

ALL_FACTOR_KEYS = (
    [k for k, _, _ in COMMERCIAL_FACTORS]
    + [k for k, _, _ in FEASIBILITY_FACTORS]
    + [k for k, _ in GATE_FACTORS]
)

# AI 需要自己打分的因子（no_code 由 code_dependency 换算，不让 AI 重复判断）
AI_RATED_KEYS = [k for k in ALL_FACTOR_KEYS if k != "no_code"]

FACTOR_CN = {k: cn for k, cn, _ in COMMERCIAL_FACTORS + FEASIBILITY_FACTORS}
FACTOR_CN.update({k: cn for k, cn in GATE_FACTORS})


# 分数分布校准指数。>1 表示"曲线向下压"：
#   全中位数(每项3分, composite=50) → 5 分而不是 6 分，避免"平庸机会看起来还行"。
#   各项都拿 4 分(consistently good) → 8 分；只有各项都拿 5 分才够得着 10 分。
#   gamma 取 1.10 才能同时满足「中位=5」和「全4=8」这两个刻度（单一幂曲线下二者只能一起调）。
CALIBRATION_GAMMA = 1.10

# AI 至少要给出这个比例的子因子，打分才算可信；低于此值走降级封顶
MIN_FACTOR_COVERAGE = 0.6

# dbs 公理4「流量≠收入」：拿不出收入证据时，商业化潜力打的折扣
NO_REVENUE_PROOF_DISCOUNT = 0.85


def _is_rated(v: Any) -> bool:
    """判断 AI 到底有没有给这一项打分（区分"给了3分"和"压根没给"）。"""
    if v is None or isinstance(v, bool):
        return False
    try:
        float(v)
    except (TypeError, ValueError):
        return False
    return True


def _clamp(v: Any, lo: int = 1, hi: int = 5, default: int = 3) -> int:
    """把 AI 返回的任意值收敛成 1-5 的整数；缺失或非法一律给中位数 3（保守）。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _weighted(factors: dict, spec: list[tuple[str, str, float]]) -> float:
    """加权求和 → 0-100。1 分 = 0 分，5 分 = 100 分（线性映射）。"""
    total = 0.0
    for key, _cn, weight in spec:
        raw = _clamp(factors.get(key))
        total += (raw - 1) / 4.0 * 100.0 * weight
    return total


def code_dependency_to_no_code(code_dependency: int) -> int:
    """代码依赖度(1-5, 5=必须精通编程) → 不用写代码程度(1-5, 5=完全不碰代码)。"""
    cd = _clamp(code_dependency, default=3)
    return 6 - cd


def compute(
    factors: dict,
    code_dependency: int = 3,
    authenticity: int = 3,
    hype: bool = False,
) -> dict:
    """
    核心公式。输入 AI 观察到的子因子分（1-5），输出三个可解释的数字。

    参数:
      factors:         AI 回答的事实型子因子（见 FACTOR_RUBRIC）
      code_dependency: 代码依赖度 1-5
      authenticity:    真实性 1-5
      hype:            确定性关键词检测判定的「噱头文」标记（见 filters.is_hype）

    返回:
      {
        "commercial": 0-100 商业化潜力,
        "feasibility": 0-100 可行性,
        "startup_index": 1-10 适合你启动的指数,
        "composite": 0-100 综合分（排序 & 过滤用）,
        "drivers": [(中文因子名, 1-5分), ...] 最加分的 2 项,
        "drags": [(中文因子名, 1-5分), ...] 最拖后腿的 2 项,
        "caps": ["封顶原因", ...] 触发过的硬性封顶规则,
        "gates": ["dbs 检验未通过项", ...] 供卡片展示的人话结论,
        "coverage": 0-1 AI 实际给出的子因子比例,
        "degraded": bool 是否因打分依据不全而降级,
      }
    """
    f = {k: _clamp(factors.get(k)) for k in AI_RATED_KEYS}
    # no_code 不让 AI 重复打，直接由代码依赖度换算，保证与硬过滤口径一致
    f["no_code"] = code_dependency_to_no_code(code_dependency)

    commercial = _weighted(f, COMMERCIAL_FACTORS)
    feasibility = _weighted(f, FEASIBILITY_FACTORS)
    auth = _clamp(authenticity)

    # ---- dbs 公理4：流量 ≠ 收入 ----
    # 只有粉丝量/播放量、拿不出真金白银的收入证据 → 商业化潜力直接打折，
    # 而不是等着它在加权里被稀释掉。
    if f["revenue_proof"] <= 2:
        commercial *= NO_REVENUE_PROOF_DISCOUNT

    # 综合分：可行性 45% + 商业化 35% + 真实性 20%
    #   —— 用户是「照着抄」的角度，能不能落地 > 生意天花板 > 故事可不可信
    composite = 0.45 * feasibility + 0.35 * commercial + 0.20 * ((auth - 1) / 4.0 * 100.0)

    # 适合你启动指数：0-100 综合分经 gamma 校准后压到 1-10
    #   线性映射会让"全中位数"拿到 5.5→6 分，显得比实际好；gamma>1 把中段压下去
    idx = (max(composite, 0.0) / 100.0) ** CALIBRATION_GAMMA * 9.0 + 1.0

    # ============================================================
    # 硬性封顶（一票压制，防止"总分虚高"掩盖致命短板）
    # dbs 的精髓是「否决」而不是「加权」——下面每一条都对应一条公理或检验
    # ============================================================
    caps: list[str] = []
    gates: list[str] = []

    # 降级保护：AI 没按格式给够子因子时，全部按中位数 3 计算会虚高，必须压到门槛线
    rated = sum(1 for k in AI_RATED_KEYS if _is_rated(factors.get(k)))
    coverage = rated / len(AI_RATED_KEYS) if AI_RATED_KEYS else 1.0
    degraded = coverage < MIN_FACTOR_COVERAGE
    if degraded:
        idx = min(idx, 4.0)
        caps.append(f"AI 未给全打分依据（{rated}/{len(AI_RATED_KEYS)}项）→ 封顶4分")

    # ---- dbs 产品颜色测试：说不出产品是什么颜色的，就还没进入市场 ----
    if f["concrete"] <= 2:
        idx = min(idx, 3.0)
        caps.append("说不清具体卖什么给谁 → 封顶3分")
        gates.append("产品颜色测试未过：只有方向没有产品")

    # ---- dbs 检验1 印钞机 / 检验6 规模化：换个人喂同样的料，出不出同样的货 ----
    if f["machine"] <= 2:
        idx = min(idx, 4.0)
        caps.append("只有原作者本人能做 → 封顶4分")
        gates.append("印钞机检验未过：靠个人魅力/独家关系，换人就失效")

    # ---- dbs 公理4：流量 ≠ 收入 ----
    if f["revenue_proof"] <= 2:
        idx = min(idx, 6.0)
        gates.append("只有流量数据、没有收入证据（粉丝多≠赚钱）")

    # ---- dbs 公理5：定价即产品。没有定价，就还没有产品 ----
    if f["price_ladder"] <= 1 and f["pricing"] <= 2:
        idx = min(idx, 5.0)
        caps.append("没有定价 = 还没有产品 → 封顶5分")
        gates.append("定价检验未过：全文没有一个真实价格")

    # ---- dbs 筛子2「你能看懂吗」：获客→转化→交付→复购的链条 ----
    if f["channel"] <= 2:
        idx = min(idx, 6.0)
        caps.append("没写清客户从哪来 → 封顶6分")
    if f["delivery_chain"] <= 2:
        idx = min(idx, 6.0)
        gates.append("链条看不懂：只讲了获客，没讲怎么成交和交付")

    # ---- 已有红线：技术门槛 / 卖铲子 ----
    if _clamp(code_dependency) >= 4:
        idx = min(idx, 3.0)
        caps.append("需要写代码 → 封顶3分")
    if auth <= 2:
        idx = min(idx, 3.0)
        caps.append("疑似卖课/卖铲子 → 封顶3分")
    if f["urgency"] <= 2 and f["pricing"] <= 2:
        idx = min(idx, 5.0)
        caps.append("需求不痛且卖不上价 → 封顶5分")

    # ---- dbs 语言陷阱：满篇"风口/赛道/前景"这类没有定义的词 ----
    if hype:
        idx = min(idx, 5.0)
        caps.append("满篇风口/赛道等空词 → 封顶5分")
        gates.append("语言陷阱：核心词没有定义，问题本身不成立")

    startup_index = int(max(1, min(10, round(idx))))

    # drivers/drags 只从参与加权的因子里挑，闸门因子单独在 gates 里说
    weighted_keys = {k for k, _, _ in COMMERCIAL_FACTORS + FEASIBILITY_FACTORS}
    ranked = sorted(
        ((FACTOR_CN[k], v) for k, v in f.items() if k in weighted_keys),
        key=lambda x: x[1],
        reverse=True,
    )
    drivers = [x for x in ranked if x[1] >= 4][:2]
    drags = [x for x in reversed(ranked) if x[1] <= 2][:2]

    return {
        "commercial": int(round(commercial)),
        "feasibility": int(round(feasibility)),
        "startup_index": startup_index,
        "composite": round(composite, 1),
        "drivers": drivers,
        "drags": drags,
        "caps": caps,
        "gates": gates,
        "factors": f,
        "coverage": round(coverage, 2),
        "degraded": degraded,
    }


def explain(result: dict) -> str:
    """把打分结果压成一句中文人话，放在卡片上让用户知道分数怎么来的。"""
    parts = []
    if result.get("drivers"):
        parts.append("加分：" + "、".join(f"{cn}{v}分" for cn, v in result["drivers"]))
    if result.get("drags"):
        parts.append("扣分：" + "、".join(f"{cn}仅{v}分" for cn, v in result["drags"]))
    if result.get("caps"):
        parts.append("；".join(result["caps"]))
    return " · ".join(parts)


def gate_summary(result: dict) -> str:
    """dbs 检验未通过项，单独一行放在卡片上（这是最该让用户看到的"为什么不行"）。"""
    gates = result.get("gates") or []
    if not gates:
        return ""
    return "；".join(gates)


def apply_to_item(item, factors: dict) -> dict:
    """把打分结果写回 ContentItem（就地修改），并返回原始结果字典。"""
    res = compute(
        factors,
        code_dependency=getattr(item, "code_dependency", 3) or 3,
        authenticity=getattr(item, "authenticity", 3) or 3,
        hype=bool(getattr(item, "hype_flag", False)),
    )
    item.score_factors = res["factors"]
    item.commercial_score = res["commercial"]
    item.feasibility_score = res["feasibility"]
    item.startup_index = res["startup_index"]
    item.score_reason = explain(res)
    item.gate_reason = gate_summary(res)
    # 综合相关度改由公式给出（0-1），替代 AI 自己拍的 relevance_score
    item.relevance_score = round(res["composite"] / 100.0, 3)
    return res


# ============================================================
# 给 AI 提示词用的因子说明（保证提示词与公式永远同源，改一处就够）
# ============================================================

FACTOR_RUBRIC = """### A. 商业化潜力子项（判断「这门生意本身值不值钱」，与你会不会做无关）

- **urgency 需求紧迫度**：5=客户现在就在流血找人解决（比如店铺没人进、报税截止在即）；
  4=明确痛点，愿意排期解决；3=有需求但不急；2=锦上添花；1=纯自嗨、客户根本没意识到。
- **pricing 单笔收费**：5=单笔 ≥1万元；4=2000-1万元；3=300-2000元；
  2=50-300元；1=<50元或免费引流。
- **margin 交付毛利**：5=交付几乎不花时间（现成资料/自动发货）；4=少量人工；
  3=需要投入半天到一天；2=重人工、按小时换钱；1=倒贴时间或需垫资。
- **repeat 持续付费**：5=按月订阅/长期陪跑；4=高频复购（每季度）；3=偶尔回购；
  2=一次性但客单高；1=一锤子买卖且客单低。
- **price_ladder 定价结构**：原文有没有交代价格体系？
  5=同时有便宜的引流款和贵的利润款，且价差在 5 倍以上（这是成熟的定价设计）；
  4=有两个以上价格，但价差不到 5 倍；3=只有一个明确价格；
  2=只说了"收费"但没有具体数字；1=全文没有任何价格信息。
- **revenue_proof 收入证据**：原文拿出来的是钱，还是流量？
  5=有具体收入数字 + 成交明细/后台截图/客户名单，能对得上；
  4=有具体收入数字但没有旁证；3=提到赚钱但金额模糊（"月入不错"）；
  2=只有粉丝数、播放量、点赞数这类流量数据，没有收入；
  1=连流量数据都没有，纯讲道理。
  ⚠️ 注意：粉丝多不等于赚钱。只有流量数据的一律给 2 分，不要因为数字大就给高分。
- **market_size 客户规模**：5=全国/全球几十万潜在客户且能列出名单（如所有本地美容院）；
  4=数万；3=数千；2=几百且分散难找；1=极小众或压根找不到人。
- **evergreen 抗过时**：5=十年后照样有人要（做饭、报税、找工作）；4=至少三五年稳；
  3=看行业周期；2=依赖某个平台的短期红利；1=蹭一波热点就没了。

### B. 可行性子项（判断「一个完全不会写代码的普通人能不能落地」）

- **channel 获客路径清晰**：5=原文写清了具体渠道+具体动作+第一批客户从哪来，
  照抄就能做（如「在某平台搜本地装修公司、发了20封私信、成交3单」）；
  4=写了渠道但动作略粗；3=只说了平台没说方法；2=只说"靠口碑/靠流量"；
  1=完全没提获客，或靠一条偶然爆款（不可复制）。
- **machine 换人也能做**：把这门生意想成一台机器——换一个普通人来喂同样的料，
  能不能产出同样的结果？
  5=完全靠流程，谁照着做都行，甚至能招个人替你做；
  4=大部分靠流程，少量依赖手感；3=需要一定经验但能练出来；
  2=严重依赖作者本人的名气、人脉、独家关系或多年积累；
  1=只有他能做（明星/网红/持牌人士/家里有资源）。
  ⚠️ 这一项是替读者问的：「这台机器换我来喂料，还转不转？」不转就没有参考价值。
- **delivery_chain 交付链路清晰**：获客之后的三段——怎么让人掏钱（转化）、
  东西怎么送到客户手上（交付）、客户会不会再买（复购）——原文讲清了几段？
  5=三段全讲清；4=讲清两段；3=讲清一段；2=只有结果没有过程；1=完全没提。
- **replicable 可复刻性**：一个新人从零开始照抄，多久能做出个样子？
  5=方法完全公开、工具都是现成的，一周内能复刻出雏形；
  4=需要摸索但没有卡点；3=有一两个环节要自己试；
  2=依赖执照、渠道授权、独家货源或长期人脉，普通人卡死；
  1=根本抄不了（平台不再开放、政策已变、机会窗口已关）。
  ⚠️ 注意：这一项和「竞争壁垒」是反的。壁垒越高，你越抄不了，分越低。
  不要因为"这生意别人抢不走"就给高分——那说明你也抢不到。
- **capital 启动资金低**：5=0 成本；4=几百块工具费；3=一两千；2=数千到一万；1=需要备货/租场地/大额投入。
- **speed 出单速度快**：5=一周内能试出第一单；4=一个月内；3=两三个月；2=半年；1=一年以上或需长期养号。
- **skill 技能门槛低**：5=只需要说人话、发帖子、和人沟通；4=需要练一下但一周能上手（剪辑、排版）；
  3=需要一两个月学习；2=需要专业资质/执照/多年经验；1=必须有硬技术背景。

### C. 闸门项（这一项不参与加分，只用来一票压制）

- **concrete 具体性**：能不能说出这门生意到底卖什么东西给什么人、多少钱？
  （检验方法：如果这是个实物，你能说出它是什么颜色的吗？说不出来，就说明还没进入市场。）
  5=产品、买家、价格三样全都具体到可以直接照做；
  4=三样里有两样具体；3=只有一样具体；
  2=通篇是"方向""领域""模式"这类词，落不到一个具体东西上；
  1=纯概念空转（如「AI 是个大机会」「做自媒体能赚钱」）。

---

**打分纪律（违反就等于这条机会白采集了）**：
1. 只根据原文能观察到的事实打分。原文没写清楚的一律给保守分（2 或 3），不要脑补。
2. 不要因为「这个方向听起来不错」就给高分——听起来不错本身不是证据。
3. 看到大数字先分清是「收入」还是「流量」。粉丝十万、播放百万，如果没有收入，
   revenue_proof 只能给 2 分。
4. 看到「别人抄不走」「有壁垒」「独家资源」，replicable 要给低分，不是高分。
5. channel（获客路径）和 machine（换人也能做）是所有子项里最重要的两项，
   宁可给低不要给高。"""
