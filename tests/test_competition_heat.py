"""竞争热度软信号测试（用户 2026-08-11）

原则：高频曝光 = 可能红海、卖铲子扎堆，但「高频」是软信号——
  · 只降权（排序时扣分的「有效启动指数」）+ 标注（⚠️ 红海）
  · 绝不改原始 startup_index，因此不会被溢池 quality_threshold-1 门槛间接硬删
  · 红海机会会沉到后面；超过每日 10 条上限时先被溢池延后，而非消失
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.base import ContentItem
from opportunity import (
    competition_heat, heat_penalty, effective_index, apply_competition_heat,
)
from overflow_pool import OverflowPool


def _item(url, si, repeat=0, corro=0):
    it = ContentItem(title="t", url=url)
    it.startup_index = si
    it.repeat_count = repeat
    it.corroborations = corro
    return it


def test_heat_low():
    assert competition_heat(_item("a", 8)) == 0
    assert competition_heat(_item("a", 8, repeat=1, corro=1)) == 0


def test_heat_warm():
    assert competition_heat(_item("a", 8, repeat=2)) == 1
    assert competition_heat(_item("a", 8, corro=3)) == 1


def test_heat_red():
    assert competition_heat(_item("a", 8, repeat=3)) == 2
    assert competition_heat(_item("a", 8, corro=4)) == 2


def test_penalty_and_effective():
    warm = _item("a", 8, repeat=2)
    assert heat_penalty(warm) == 1 and effective_index(warm) == 7
    red = _item("a", 8, repeat=3)
    assert heat_penalty(red) == 2 and effective_index(red) == 6
    low = _item("a", 8)
    assert effective_index(low) == 8


def test_apply_sets_flag():
    items = [_item("a", 9, repeat=3), _item("b", 5, corro=1)]
    apply_competition_heat(items)
    assert items[0].red_ocean is True and items[0].heat_penalty == 2
    assert items[1].red_ocean is False and items[1].heat_penalty == 0


def test_overflow_soft_downrank_sinks_red_ocean():
    """红海(raw 9/eff 7) 应沉到普通(raw 8/eff 8) 之后，caps=1 时普通胜出。"""
    with tempfile.TemporaryDirectory() as d:
        pool = OverflowPool(Path(d) / "o.json", daily_cap=1, quality_threshold=7)
        pushed, _ = pool.decide(
            [_item("red", 9, repeat=3), _item("norm", 8, corro=1)],
            "2026-08-11",
        )
        assert [it.url for it in pushed] == ["norm"], \
            f"红海应沉底，实际 {[it.url for it in pushed]}"


def test_overflow_does_not_hardkill_red_ocean():
    """红海不被硬删：caps=2 时红海(raw 9) 与普通(raw 8) 都应推送（raw≥6）。"""
    with tempfile.TemporaryDirectory() as d:
        pool = OverflowPool(Path(d) / "o.json", daily_cap=2, quality_threshold=7)
        pushed, _ = pool.decide(
            [_item("red", 9, repeat=3), _item("norm", 8, corro=1)],
            "2026-08-11",
        )
        assert {it.url for it in pushed} == {"red", "norm"}, \
            f"红海不应被硬删，实际 {[it.url for it in pushed]}"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
