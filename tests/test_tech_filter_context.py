"""技术过滤器上下文豁免测试

背景（2026-08-09 线上误伤）：
`is_technical()` 原本是裸子串匹配，把「无需编程」「不用写代码」这类
**最该推给用户**的内容全部误杀；「从程序员被裁到小红书博主」这种
转行故事讲的是非技术生意，也因为提到过去职业身份被砍。

这批用例把「该通过 / 该拦截」两个方向都钉死，防止以后调关键词时改坏。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filters import is_technical, technical_reason  # noqa: E402


# ------------------------------------------------------------
# 应该通过（非技术内容，不许误杀）
# ------------------------------------------------------------

NON_TECHNICAL_CASES = [
    # 否定式：这恰恰是「非技术」的最强信号
    "无需编程，用Notion模板月入3000",
    "不用写代码也能做的副业",
    "零基础不懂编程，靠小红书接单",
    "我不是程序员，也做出了自己的产品",
    "完全不需要写代码，全程用现成工具",
    "不会编程也能做的10个小生意",
    "没有技术背景，靠写作月入2万",
    # 英文否定式
    "no code needed: build a newsletter business",
    "I am not a software engineer but I sell templates",
    "grow a business without coding",
    # 转行叙事：讲的是非技术生意
    "从程序员被裁到81万粉小红书博主",
    "前端工程师转行做手工皂，月入2万",
    "她曾是软件工程师，现在全职做手账",
    "软件开发离职后，我开了家线上花店",
    # 纯非技术
    "Notion模板赚钱实战：从设计到上架Gumroad全流程",
    "宝妈在小红书卖手工素材，月入8000",
]


# ------------------------------------------------------------
# 应该拦截（技术内容，不许漏网）
# ------------------------------------------------------------

TECHNICAL_CASES = [
    "教你写代码接单赚钱",
    "独立开发者如何靠 SaaS 月入1万美金",
    "程序员必看：用Python自动化你的工作流",
    "我是全栈工程师，分享我的技术架构",
    "indie hacker builds an app in 30 days",
    "machine learning engineer shares his stack",
    # 边界：否定/转行词的「假邻居」，不能被错误豁免
    "目前正在做全栈开发，分享心得",          # 「目前」含「前」，但不是否定
    "他现在还是程序员，边上班边写代码接单",   # 「现在」不是过去时
]


def test_non_technical_not_blocked():
    """非技术内容不许被误判为技术。"""
    wrong = [c for c in NON_TECHNICAL_CASES if is_technical(c)]
    assert not wrong, "以下非技术内容被误杀: " + "; ".join(
        f"{c} (命中 {technical_reason(c)})" for c in wrong
    )


def test_technical_still_blocked():
    """真正的技术内容必须仍被拦截。"""
    wrong = [c for c in TECHNICAL_CASES if not is_technical(c)]
    assert not wrong, "以下技术内容漏网: " + "; ".join(wrong)


def test_negation_window_is_bounded():
    """否定词离得太远时不应豁免——否则一句『无需』能赦免整段文字。"""
    # 「无需」在开头，技术词在很后面，中间隔了大段无关内容
    text = "无需任何门槛就能开始做自己的小生意，" + "从零起步慢慢积累客户口碑，" * 3 + "我是资深程序员"
    assert is_technical(text), "远距离否定词不应豁免后面的裸技术信号"


def test_career_shift_window_is_bounded():
    """转行词离得太远时不应豁免。"""
    text = "我是程序员，每天写代码调试上线，" + "维护线上服务处理告警，" * 3 + "后来才转行"
    assert is_technical(text), "远距离转行词不应豁免前面的裸技术信号"


def test_mixed_text_blocks_when_any_bare_signal():
    """一处豁免不能赦免全文：只要还有裸技术信号就该拦截。"""
    text = "虽然无需编程就能上手，但我本职是后端工程师，也接开发外包"
    assert is_technical(text)


def test_technical_reason_reports_hits():
    """技术判定要能报出命中词，便于排查误伤。"""
    assert "程序员" in technical_reason("程序员必看：自动化工作流")
    assert technical_reason("无需编程，用模板赚钱") == ""


def test_empty_and_none_safe():
    assert is_technical("") is False
    assert is_technical(None) is False
    assert technical_reason("") == ""


# ------------------------------------------------------------

def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
