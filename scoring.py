"""
客观商业打分引擎 (Objective Business Scoring)

设计原则 —— 「AI 只做观察，公式做判断」：
  ❌ 旧做法：让 AI 直接说「这条值 0.85 分」→ 主观、不稳定、无法解释、不可比较。
  ✅ 新做法：让 AI 只回答 11 个「可从原文观察到的事实型子问题」（每个 1-5 分，
     每档都有明确锚点定义），分数怎么加权、怎么封顶，全部写死在下面的 Python 公式里。
     同一篇文章，今天跑和明天跑得到同样的分；不同文章之间可以横向比较。

打分体系来源（公开可查的成熟框架，非自创）：
  1. Josh Kaufman《The Personal MBA》「十项经济性评估」(Ten Ways to Evaluate a Market)：
     紧迫度 / 市场规模 / 定价潜力 / 获客成本 / 交付成本 / 独特性 / 上市速度 /
     前期投入 / 追加销售潜力 / 长青度。
  2. 独立创业圈流行的 BB Score（10 因子加权 0-100）：Build Difficulty / Market Size /
     Competition / Defensibility / Sales Cycle / LTV / Validation Speed /
     Capital Intensity / Distribution / Unit Economics。
本文件把两套框架合并、去掉「需要写代码才有意义」的因子（如 Build Difficulty 里的技术
部分改成「是否必须写代码」），并按本项目用户画像（完全不会写代码）重新分配权重。

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
COMMERCIAL_FACTORS: list[tuple[str, str, float]] = [
    ("urgency",     "需求紧迫度",   0.20),  # 客户是"现在就疼"还是"有了更好"
    ("market_size", "客户规模",     0.18),  # 这样的客户全网有多少、找不找得到
    ("pricing",     "单笔收费",     0.18),  # 一单能收多少钱
    ("repeat",      "持续付费",     0.14),  # 一次性买断 vs 每月续费
    ("moat",        "竞争壁垒",     0.12),  # 做的人多不多、客户换人容不容易
    ("margin",      "交付毛利",     0.10),  # 每单要搭进去多少时间和成本
    ("evergreen",   "抗过时",       0.08),  # 明年这个需求还在不在
]

# ---- 可行性：你（不会写代码的人）能不能真的落地 ----
FEASIBILITY_FACTORS: list[tuple[str, str, float]] = [
    ("no_code", "不用写代码", 0.28),  # 由 code_dependency 反算，AI 不单独打
    ("channel", "获客路径清晰", 0.24),  # 原文有没有写清客户从哪来、你能不能照做
    ("capital", "启动资金低", 0.16),  # 0 成本起步 = 5
    ("speed",   "出单速度快", 0.16),  # 一周内能试出水花 vs 要熬几个月
    ("skill",   "所需技能你已有", 0.16),  # 写字/拍照/沟通 vs 需要执照或专业资质
]

ALL_FACTOR_KEYS = [k for k, _, _ in COMMERCIAL_FACTORS] + [
    k for k, _, _ in FEASIBILITY_FACTORS
]

# AI 需要自己打分的因子（no_code 由 code_dependency 换算，不让 AI 重复判断）
AI_RATED_KEYS = [k for k in ALL_FACTOR_KEYS if k != "no_code"]

FACTOR_CN = {k: cn for k, cn, _ in COMMERCIAL_FACTORS + FEASIBILITY_FACTORS}


# 分数分布校准指数。>1 表示"曲线向下压"：
#   全中位数(每项3分, composite=50) → 5 分而不是 6 分，避免"平庸机会看起来还行"。
#   只有真正各项都强的机会才够得着 8-10 分。
CALIBRATION_GAMMA = 1.3

# AI 至少要给出这个比例的子因子，打分才算可信；低于此值走降级封顶
MIN_FACTOR_COVERAGE = 0.6


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
) -> dict:
    """
    核心公式。输入 AI 观察到的子因子分（1-5），输出三个可解释的数字。

    返回:
      {
        "commercial": 0-100 商业化潜力,
        "feasibility": 0-100 可行性,
        "startup_index": 1-10 适合你启动的指数,
        "composite": 0-100 综合分（排序 & 过滤用）,
        "drivers": [(中文因子名, 1-5分), ...] 最加分的 2 项,
        "drags": [(中文因子名, 1-5分), ...] 最拖后腿的 2 项,
        "caps": ["封顶原因", ...] 触发过的硬性封顶规则,
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

    # 综合分：可行性 45% + 商业化 35% + 真实性 20%
    #   —— 用户是「照着抄」的角度，能不能落地 > 生意天花板 > 故事可不可信
    composite = 0.45 * feasibility + 0.35 * commercial + 0.20 * ((auth - 1) / 4.0 * 100.0)

    # 适合你启动指数：0-100 综合分经 gamma 校准后压到 1-10
    #   线性映射会让"全中位数"拿到 5.5→6 分，显得比实际好；gamma>1 把中段压下去
    idx = (max(composite, 0.0) / 100.0) ** CALIBRATION_GAMMA * 9.0 + 1.0

    # ---- 硬性封顶（一票压制，防止"总分虚高"掩盖致命短板）----
    caps: list[str] = []

    # 降级保护：AI 没按格式给够子因子时，全部按中位数 3 计算会虚高，必须压到门槛线
    rated = sum(1 for k in AI_RATED_KEYS if _is_rated(factors.get(k)))
    coverage = rated / len(AI_RATED_KEYS) if AI_RATED_KEYS else 1.0
    degraded = coverage < MIN_FACTOR_COVERAGE
    if degraded:
        idx = min(idx, 4.0)
        caps.append(f"AI 未给全打分依据（{rated}/{len(AI_RATED_KEYS)}项）→ 封顶4分")

    if _clamp(code_dependency) >= 4:
        idx = min(idx, 3.0)
        caps.append("需要写代码 → 封顶3分")
    if auth <= 2:
        idx = min(idx, 3.0)
        caps.append("疑似卖课/卖铲子 → 封顶3分")
    if f["channel"] <= 2:
        idx = min(idx, 6.0)
        caps.append("没写清客户从哪来 → 封顶6分")
    if f["urgency"] <= 2 and f["pricing"] <= 2:
        idx = min(idx, 5.0)
        caps.append("需求不痛且卖不上价 → 封顶5分")

    startup_index = int(max(1, min(10, round(idx))))

    ranked = sorted(
        ((FACTOR_CN[k], v) for k, v in f.items()),
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


def apply_to_item(item, factors: dict) -> dict:
    """把打分结果写回 ContentItem（就地修改），并返回原始结果字典。"""
    res = compute(
        factors,
        code_dependency=getattr(item, "code_dependency", 3) or 3,
        authenticity=getattr(item, "authenticity", 3) or 3,
    )
    item.score_factors = res["factors"]
    item.commercial_score = res["commercial"]
    item.feasibility_score = res["feasibility"]
    item.startup_index = res["startup_index"]
    item.score_reason = explain(res)
    # 综合相关度改由公式给出（0-1），替代 AI 自己拍的 relevance_score
    item.relevance_score = round(res["composite"] / 100.0, 3)
    return res


# ============================================================
# 给 AI 提示词用的因子说明（保证提示词与公式永远同源，改一处就够）
# ============================================================

FACTOR_RUBRIC = """### 商业化潜力子项（判断「这门生意本身值不值钱」，与你会不会做无关）

- **urgency 需求紧迫度**：5=客户现在就在流血找人解决（比如店铺没人进、报税截止在即）；
  4=明确痛点，愿意排期解决；3=有需求但不急；2=锦上添花；1=纯自嗨、客户根本没意识到。
- **market_size 客户规模**：5=全国/全球几十万潜在客户且能列出名单（如所有本地美容院）；
  4=数万；3=数千；2=几百且分散难找；1=极小众或压根找不到人。
- **pricing 单笔收费**：5=单笔 ≥1万元；4=2000-1万元；3=300-2000元；
  2=50-300元；1=<50元或免费引流。
- **repeat 持续付费**：5=按月订阅/长期陪跑；4=高频复购（每季度）；3=偶尔回购；
  2=一次性但客单高；1=一锤子买卖且客单低。
- **moat 竞争壁垒**：5=需要独家资源/长期积累的信息差，别人抄不走；4=先发+口碑有优势；
  3=常见但执行差距大；2=遍地都是，靠压价竞争；1=已彻底红海。
- **margin 交付毛利**：5=交付几乎不花时间（现成资料/自动发货）；4=少量人工；
  3=需要投入半天到一天；2=重人工、按小时换钱；1=倒贴时间或需垫资。
- **evergreen 抗过时**：5=十年后照样有人要（做饭、报税、找工作）；4=至少三五年稳；
  3=看行业周期；2=依赖某个平台的短期红利；1=蹭一波热点就没了。

### 可行性子项（判断「一个完全不会写代码的普通人能不能落地」）

- **channel 获客路径清晰**：5=原文写清了具体渠道+具体动作+第一批客户从哪来，
  照抄就能做（如「在某平台搜本地装修公司、发了20封私信、成交3单」）；
  4=写了渠道但动作略粗；3=只说了平台没说方法；2=只说"靠口碑/靠流量"；
  1=完全没提获客，或靠一条偶然爆款（不可复制）。
- **capital 启动资金低**：5=0 成本；4=几百块工具费；3=一两千；2=数千到一万；1=需要备货/租场地/大额投入。
- **speed 出单速度快**：5=一周内能试出第一单；4=一个月内；3=两三个月；2=半年；1=一年以上或需长期养号。
- **skill 所需技能你已有**：5=只需要说人话、发帖子、和人沟通；4=需要练一下但一周能上手（剪辑、排版）；
  3=需要一两个月学习；2=需要专业资质/执照/多年经验；1=必须有硬技术背景。

打分要求：只根据原文能观察到的事实打分；原文没写清楚的一律给保守分（2 或 3），
不要脑补、不要因为"这个方向听起来不错"就给高分。"""
