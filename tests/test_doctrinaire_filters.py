#!/usr/bin/env python3
"""
死板教条筛选标准修复 — 回归测试（不碰网络、不需要 AI key）

覆盖 4 个「只看关键词、不看上下文」导致的误杀/误并：
  #87 强关键词闸门误杀素人案例（sources/base.py）
  #88 卖铲子判定否定式误杀（teardown.py）
  #90 噱头闸门被裸单字「元」架空（filters.py）
  #89 机会库相似度误并不同主题（library.py）

运行： cd daily-opportunity-bot && .venv/Scripts/python tests/test_doctrinaire_filters.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sources.base import has_strong_keyword, keyword_score
import filters
from teardown import _operator_authenticity
import library as lib_mod

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ============================================================
# #87 强关键词闸门：素人真实赚钱故事不能误杀
# ============================================================
def test_strong_keyword_bypass():
    print("\n===== #87 强关键词闸门：素人/金额旁路 =====")
    kw = {
        "strong": ["一人公司", "副业", "月入", "赚钱", "变现", "merch", "royalty"],
        "weak": ["小红书", "抖音", "博主"],
    }
    # 大白话素人案例：没有行话强词，但应放行进 AI
    person_cases = [
        "从被裁到81万粉博主，琳达不呆如何用小红书重启人生？",
        "一位宝妈靠手工皂在市集月售3000块",
        "她辞职后开线上花艺教室",
        "深圳夫妻做遛狗上门服务",
        "把爷爷老手艺搬上抖音",
    ]
    for t in person_cases:
        check(f"素人案例放行：{t[:14]}…", has_strong_keyword(t, kw))

    # 国际素人案例：具体金额 + 生意动词
    intl = "How I make 4k a month selling crochet patterns"
    check(f"国际金额+动词放行：{intl[:20]}…", has_strong_keyword(intl, kw))

    # 真正无关的内容（元宇宙噱头，无人物/金额）→ 仍应被挡
    check("无关内容仍拦截：元宇宙赛道", not has_strong_keyword("元宇宙是下一个风口赛道", kw))

    # 英文短词词边界：merch 不应命中 merchant banking
    check("merch 不误中 merchant banking",
          not has_strong_keyword("Goldman Sachs merchant banking division", kw))
    # 但真正的 merch / royalty 内容应通过（作为独立词命中）
    check("merch by amazon 正常命中",
          has_strong_keyword("merch by amazon passive income", kw))
    check("royalty 正常命中独立词",
          has_strong_keyword("earn royalty income from your photos", kw))

    # keyword_score: 触发旁路也给分（不为 0），且强词命中也给分
    check("keyword_score 旁路给分",
          keyword_score(None, "一位宝妈靠手工皂月售3000块", kw) > 0)
    check("keyword_score 强词给分",
          keyword_score(None, "普通人做一人公司月入过万", kw) > 0)
    check("keyword_score 无关内容 0 分",
          keyword_score(None, "今日天气晴，适合散步", kw) == 0.0)


# ============================================================
# #88 卖铲子判定：否定式澄清不应误杀
# ============================================================
def test_authenticity_negation():
    print("\n===== #88 卖铲子判定：否定式豁免 =====")
    # 否定式澄清 → 应判 3（不 skip），这些是最该推的真实操盘手
    negation_ok = [
        ("没有明显的割韭菜行为，靠写真本事赚钱", "最该抄的作业是内容打磨"),
        ("并非传销或资金盘，就是正常的私域卖货", "核心是把复购做起来"),
        ("未发现拉人头等问题，纯靠作品引流", "坚持做垂直内容"),
        ("这个号不是卖铲子，而是卖自己的插画服务", "靠约稿养活自己"),
    ]
    for rf, learn in negation_ok:
        check(f"否定式不误杀：{rf[:12]}…", _operator_authenticity(rf, learn) == 3)

    # 真卖铲子 → 仍判 2（skip）
    real_scam = [
        ("主要收入来自卖铲子——教别人做一人公司、卖副业课", "核心就是割韭菜式培训"),
        ("典型资金盘，拉人头返利", "别碰"),
        ("庞氏结构，先入坑的赚后入的钱", "风险极高"),
    ]
    for rf, learn in real_scam:
        check(f"真卖铲子仍 skip：{rf[:12]}…", _operator_authenticity(rf, learn) == 2)


# ============================================================
# #90 噱头闸门：裸单字「元」不能架空
# ============================================================
def test_hype_concrete_signal():
    print("\n===== #90 噱头闸门：金额信号判定 =====")
    # 这些含「元」但不是金额 → 不应被判为有具体金额信号
    false_amount = ["元宇宙是下一个风口", "公元元年历史", "元旦快乐", "元气森林新品上市"]
    for t in false_amount:
        check(f"裸『元』不误判：{t[:10]}…", not filters.has_concrete_signal(t))

    # 真有金额 → 应判有具体信号
    real_amount = ["客单价199元", "一单收费3000块", "月入5000美元", "赚到200美金刀"]
    for t in real_amount:
        check(f"真金额识别：{t[:10]}…", filters.has_concrete_signal(t))

    # 噱头门：满篇空词且无真金额 → 仍判噱头（不再被元宇宙架空）
    hype = "普通人财富自由的下一个风口，抓住时代红利就能躺赚，未来趋势必将取代传统"
    check("元宇宙式空话仍判噱头", filters.is_hype(hype))
    # 但元宇宙标题本身若没有空词堆叠，不应判噱头
    check("元宇宙科普长文不误判噱头",
          not filters.is_hype("元宇宙是基于区块链的沉浸式虚拟空间，本文从技术架构讲起"))


# ============================================================
# #89 机会库相似度：差一字的不同生意不误并
# ============================================================
def test_library_sim():
    print("\n===== #89 机会库相似度：差一字不误并 =====")
    false_merge = [
        ("手工皂市集摆摊", "手工蜡烛市集摆摊"),
        ("英语陪练一对一", "日语陪练一对一"),
        ("帮宝妈做小红书代运营", "帮宝妈做抖音代运营"),
    ]
    for a, b in false_merge:
        check(f"差一字不误并：{a[:10]}…",
              not lib_mod._is_same_topic(lib_mod._norm(a), lib_mod._norm(b)))

    # 纯语序颠倒 / 归一化相同 → 仍合并
    reorder = [
        ("手工皂市集摆摊", "手工皂摆摊市集"),
        ("代写简历199元", "代写简历一份200"),
    ]
    for a, b in reorder:
        check(f"纯语序颠倒仍合并：{a[:10]}…",
              lib_mod._is_same_topic(lib_mod._norm(a), lib_mod._norm(b)))


if __name__ == "__main__":
    test_strong_keyword_bypass()
    test_authenticity_negation()
    test_hype_concrete_signal()
    test_library_sim()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== 死板教条修复测试：{passed}/{total} 通过 =====")
    sys.exit(0 if passed == total else 1)
