"""低客单「卖时间/卖劳动力」降权 + 高客单豁免 规则测试

原则（用户 2026-08-10）：
  · 做产品 / 做服务 / 高客单的，是我们更想要的 → 优先浮到前面
  · 低客单卖时间、卖劳动力的，适当降权（排后、被上限优先裁掉），但不硬删
  · 高客单个性化服务（单笔≥2000元）虽然也是卖时间/劳动力，但客单价高，同样吃香 → 豁免

判断全部复用 dbs 打分框架已有因子（margin 交付毛利 / machine 换人也能做 /
pricing 单笔收费 / repeat 持续付费），不引入外部依赖。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import compute  # noqa: E402


def _base():
    """除被测因子外，其余都给中位数 3（保守基线）。"""
    return {
        "urgency": 3, "pricing": 3, "margin": 3, "repeat": 3, "price_ladder": 3,
        "revenue_proof": 3, "market_size": 3, "evergreen": 3,
        "no_code": 3, "channel": 3, "machine": 3, "delivery_chain": 3,
        "replicable": 3, "capital": 3, "speed": 3, "skill": 3, "concrete": 3,
    }


def _has_labor_cap(res):
    return any("低客单卖时间/卖劳动力" in c for c in res["caps"])


def test_low_ticket_labor_is_downweighted():
    """低客单 + 重劳力（按小时换钱/靠个人一锤子）→ 触发降权，startup_index 压到 5。"""
    f = _base()
    f["margin"] = 1          # 重人工、按小时换钱
    f["machine"] = 1         # 严重依赖本人
    f["repeat"] = 1         # 一锤子买卖、无复购
    f["pricing"] = 1         # 单笔 <50 元（低客单）
    res = compute(f)
    assert _has_labor_cap(res), f"应触发降权，caps={res['caps']}"
    assert res["startup_index"] <= 5, f"降权后 index 应≤5，实际 {res['startup_index']}"


def test_high_ticket_labor_is_exempt():
    """高客单个性化服务（单笔≥2000元，pricing≥4）→ 即使重劳力也豁免，不降权。"""
    f = _base()
    f["margin"] = 1          # 仍重人工（靠本人时间）
    f["machine"] = 1         # 仍靠个人
    f["repeat"] = 1         # 仍一锤子
    f["pricing"] = 5         # 但单笔≥1万（高客单）→ 豁免
    res = compute(f)
    assert not _has_labor_cap(res), f"高客单不应降权，caps={res['caps']}"


def test_productized_service_not_penalized():
    """产品化服务（高毛利、换人也能做）→ 不触发劳力降权，指数高。"""
    f = _base()
    f["margin"] = 5          # 交付几乎不花时间
    f["machine"] = 5         # 换人也能做
    f["repeat"] = 5          # 长期陪跑/订阅
    f["pricing"] = 3         # 中客单，但靠产品化服务
    res = compute(f)
    assert not _has_labor_cap(res), f"产品化服务不应降权，caps={res['caps']}"


def test_high_ticket_beats_low_ticket_same_else():
    """同样的其他因子（machine/repeat 不低），低客单被降权帽压到 5，
    高客单（pricing=5）豁免降权帽、保留自然分，故高客单指数 ≥ 低客单。"""
    low = _base(); low.update({"margin": 1, "machine": 3, "repeat": 3, "pricing": 1})
    high = _base(); high.update({"margin": 1, "machine": 3, "repeat": 3, "pricing": 5})
    r_low, r_high = compute(low), compute(high)
    assert _has_labor_cap(r_low), f"低客单应被降权，caps={r_low['caps']}"
    assert not _has_labor_cap(r_high), f"高客单应豁免，caps={r_high['caps']}"
    assert r_high["startup_index"] >= r_low["startup_index"], (
        f"高客单({r_high['startup_index']}) 应≥ 低客单({r_low['startup_index']})"
    )


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t(); print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}"); failed += 1
        except Exception as e:  # noqa
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
