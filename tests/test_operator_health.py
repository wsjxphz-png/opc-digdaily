#!/usr/bin/env python3
"""
模块1 操盘手「商业底层逻辑体检」（dbs）单元测试 —— 不碰网络、不需要 AI key。

覆盖：
  1. scoring.operator_severity   三档判定（ok / warn / skip）
  2. teardown._score_business_logic  同一道 LLM 返回的 dbs 因子 → 体检 + 分档（集成）
  3. Teardown / Operator           commercial_health 字段的序列化往返
  4. operators.get_due_for_teardown   skip 不进池、warn 排后
  5. push.feishu._build_teardown_section  卡片渲染「🩺 商业体检」+ 闸门警告

运行： cd daily-opportunity-bot && .venv/Scripts/python tests/test_operator_health.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scoring
from teardown import TeardownEngine, Teardown
from operators import Operator, OperatorRoster
from push.feishu import FeishuPusher


def _result(factors: dict, authenticity=3, hype=False):
    """用 scoring.compute 造一个真实的体检结果 dict。"""
    return scoring.compute(factors, authenticity=authenticity, hype=hype)


def test_severity_ok():
    # 全 4-5 分、有真实收入、非噱头 → 健康
    f = {k: 5 for k in scoring.AI_RATED_KEYS}
    f["machine"] = 4
    f["concrete"] = 5
    f["revenue_proof"] = 4
    res = _result(f, authenticity=3, hype=False)
    assert scoring.operator_severity(res, authenticity=3, hype=False) == "ok"


def test_severity_warn_machine_only():
    # 只靠个人IP（印钞机检验未过），但其他健康 → 标注+降权，不跳过
    f = {k: 4 for k in scoring.AI_RATED_KEYS}
    f["machine"] = 2
    f["concrete"] = 4
    res = _result(f, authenticity=3, hype=False)
    assert "印钞机" in "".join(res.get("gates", []))
    assert scoring.operator_severity(res, authenticity=3, hype=False) == "warn"


def test_severity_skip_shovel():
    # red_flag 明确「主要靠教别人赚钱 / 卖铲子」→ 直接不推
    f = {k: 5 for k in scoring.AI_RATED_KEYS}
    res = _result(f, authenticity=2, hype=False)
    assert scoring.operator_severity(res, authenticity=2, hype=False) == "skip"


def test_severity_skip_hype():
    # 满篇空词的语言陷阱 → 直接不推
    f = {k: 4 for k in scoring.AI_RATED_KEYS}
    f["concrete"] = 4
    res = _result(f, authenticity=3, hype=True)
    assert scoring.operator_severity(res, authenticity=3, hype=True) == "skip"


def test_severity_skip_no_product():
    # 连产品是什么颜色都说不出（纯概念空转）→ 直接不推
    f = {k: 4 for k in scoring.AI_RATED_KEYS}
    f["concrete"] = 1
    res = _result(f, authenticity=3, hype=False)
    assert scoring.operator_severity(res, authenticity=3, hype=False) == "skip"


def test_score_business_logic_integration_warn():
    eng = TeardownEngine(None)
    op = Operator("@x", "测试人", "国际", ["@x"], ["twitter"], tech_barrier="无")
    td = Teardown(operator_handle="@x", operator_name="测试人", region="国际",
                  red_flag="", learn="", tech_barrier="无")
    data = {"dbs": {k: 4 for k in scoring.AI_RATED_KEYS}}
    data["dbs"]["machine"] = 2   # 靠个人IP → warn
    sev = eng._score_business_logic(op, data, td)
    assert sev == "warn"
    assert op.commercial_health is not None
    assert op.commercial_severity == "warn"
    assert td.commercial_health is not None
    assert op.commercial_health["gate_reason"]  # 有闸门警告文案


def test_score_business_logic_integration_skip():
    eng = TeardownEngine(None)
    op = Operator("@y", "卖课人", "国内", ["@y"], ["rss"], tech_barrier="低")
    td = Teardown(operator_handle="@y", operator_name="卖课人", region="国内",
                  red_flag="他主要靠教别人做一人公司赚钱（卖铲子）", learn="",
                  tech_barrier="低")
    data = {"dbs": {k: 5 for k in scoring.AI_RATED_KEYS}}
    sev = eng._score_business_logic(op, data, td)
    assert sev == "skip"
    assert op.commercial_severity == "skip"


def test_teardown_roundtrip_health():
    td = Teardown(operator_handle="@z", operator_name="Z", region="国际",
                  commercial_health={"commercial": 80, "feasibility": 70,
                                     "startup_index": 8, "gate_reason": ""})
    d = td.to_dict()
    td2 = Teardown.from_dict(d)
    assert td2.commercial_health == td.commercial_health


def test_operator_roundtrip_health():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "operators.json"
    roster = OperatorRoster(p)
    op = Operator("@a", "A", "国际", ["@a"], ["twitter"], tech_barrier="无")
    op.commercial_health = {"commercial": 60, "feasibility": 55, "startup_index": 6}
    op.commercial_severity = "warn"
    roster.operators["@a"] = op
    roster.save()
    roster2 = OperatorRoster(p)
    roster2.load()
    op2 = roster2.operators["@a"]
    assert op2.commercial_severity == "warn"
    assert op2.commercial_health["startup_index"] == 6


def test_rotation_filters_skip_and_deprioritizes_warn():
    roster = OperatorRoster(ROOT / "storage" / "_nonexistent_ops.json")
    ok_op = Operator("@ok", "健康", "国际", ["@ok"], ["twitter"], tech_barrier="无")
    ok_op.commercial_severity = "ok"
    warn_op = Operator("@warn", "存疑", "国际", ["@warn"], ["twitter"], tech_barrier="无")
    warn_op.commercial_severity = "warn"
    skip_op = Operator("@skip", "坏案例", "国际", ["@skip"], ["twitter"], tech_barrier="无")
    skip_op.commercial_severity = "skip"
    for o in (ok_op, warn_op, skip_op):
        roster.operators[o.handle] = o

    due = roster.get_due_for_teardown(10)
    handles = [o.handle for o in due]
    assert "@skip" not in handles, "结构性坏案例不应进入待拆池"
    assert handles.index("@ok") < handles.index("@warn"), "健康案例应排在存疑之前"


def test_feishu_renders_health():
    pusher = FeishuPusher(webhook_url="https://open.feishu.cn/xxx", dry_run=True)
    td = {
        "operator_name": "演示", "region": "国际", "replicability": 4,
        "tech_barrier": "无", "doable": "能",
        "who": "x", "deliverable": "y", "business_model": "z",
        "acquisition": "a", "stack": "b", "first_step": "c",
        "red_flag": "d", "learn": "e", "signals_used": 3,
        "commercial_health": {
            "commercial": 72, "feasibility": 65, "startup_index": 7,
            "gate_reason": "印钞机检验未过：靠个人魅力/独家关系，换人就失效",
        },
    }
    elements = pusher._build_teardown_section(td)
    text = " ".join(
        e.get("text", {}).get("content", "") for e in elements
        if e.get("tag") == "div"
    )
    assert "商业体检" in text
    assert "72" in text and "65" in text
    assert "商业底层逻辑存疑" in text
    assert "印钞机检验未过" in text


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
