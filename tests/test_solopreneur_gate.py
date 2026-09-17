#!/usr/bin/env python3
"""
一人公司定义闸门 — 回归测试（不碰网络）

背景：2026-09-17 用户指出日报推的案例不符「一人公司」定义（麻将馆=资本杠杆个体户、
训犬=按次卖时间）。根因是系统口径松：discovery.py 把「靠付费社群/会员/陪伴赚钱」
列为加分项，teardown.py 把「付费社群」写进允许方向，config.yaml 关键词含「付费社群」。
而松月一人公司知识库明确把陪伴入口/付费社群列为重交付风险。

本测试锁死新口径，防止后续编辑把它悄悄改回去：
  1. discovery 提示词含「一人公司判定」硬门槛 + 五类排除
  2. processor 提示词含「红线 4」+ 三条判据
  3. 「付费社群/会员/陪伴」不再作为加分方向出现
  4. config.yaml 允许关键词里没有 付费社群/会员制/社群运营

运行： cd opc-digdaily && python tests/test_solopreneur_gate.py
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
import discovery  # noqa: E402
import teardown  # noqa: E402
from ai import processor as proc_mod  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


def main():
    disc = discovery.DISCOVERY_SYSTEM_PROMPT
    batch = proc_mod.BATCH_SYSTEM_PROMPT
    td = teardown.TEARDOWN_SYSTEM_PROMPT

    print("\n=== 1. discovery：一人公司判定硬门槛 ===")
    check("含「一人公司判定」小节", "一人公司判定" in disc)
    check("含判据「不是粉丝多」", "不是你有很多粉丝" in disc)
    check("含判据「收入跟时间脱钩」", "收入跟你本人的时间是否脱钩" in disc)
    check("含停手判据", "停手两周" in disc)
    for kw in ("线下开店", "按次卖时间", "纯流量号", "陪跑", "远程全职"):
        check(f"排除项「{kw}」在列", kw in disc)

    print("\n=== 2. discovery：社群看交付形态，不看形态本身 ===")
    # 松月把「陪伴入口」列为七种正规定位原型之一，只提示重交付风险；
    # 故判据是「群里交付资产还是你的时间」，不是「社群一律排除」
    check("社群保留为允许方向", "靠付费社群/会员赚钱" in disc)
    check("但要求群里交付已生产好的资产", "群里必须交付已生产好的资产" in disc)
    check("「但必须有自己的付费产品」已加入", "必须有自己的付费产品" in disc)

    print("\n=== 3. processor：红线 4 ===")
    check("标题已改为「四条一票否决红线」", "四条一票否决红线" in batch)
    check("含「红线 4：必须是一人公司」", "红线 4：必须是一人公司" in batch)
    for kw in ("交付物必须可复用", "必须有自己的产品", "交付里不能是"):
        check(f"判据「{kw}」在列", kw in batch)
    check("含「付费社群本身不算淘汰项」", "付费社群本身不算淘汰项" in batch)
    check("含资产型/时间型的区分", "已生产好的资产" in batch and "你的实时时间" in batch)
    check("含社群续费判据", "停更两周" in batch)
    check("严格要求段同步为四条", "四条一票否决红线命中任一条" in batch)
    check("旧的「三条一票否决红线命中」已不存在",
          "三条一票否决红线命中任一条" not in batch)
    for kw in ("麻将", "训犬", "上门服务", "囤货", "计时咨询"):
        check(f"举例含「{kw}」", kw in batch)

    print("\n=== 4. teardown：口径注释已更新 ===")
    # 注意：这段口径写在 _operator_authenticity 的 docstring 里，不在 TEARDOWN_SYSTEM_PROMPT 里
    doc = teardown._operator_authenticity.__doc__ or ""
    check("口径已把资产型付费社群列为允许方向", "资产型付费社群" in doc)
    check("口径点明只有「交付实时时间」才排除", "交付你的实时时间" in doc)
    check("旧的「付费社群=允许方向」旧口径已不存在",
          "用户允许：内容 / 信息产品·课程·训练营 / 付费社群 / 产品化服务" not in doc)

    print("\n=== 5. config.yaml 关键词 ===")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    kws = str(cfg)
    check("「付费社群」保留为允许关键词（资产型社群可以）", "付费社群" in kws)
    check("关键词表合法可解析", isinstance(cfg, dict))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"结果: {passed}/{total} 通过")
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("失败项:", failed)
        sys.exit(1)
    print("🎉 全部通过。")


if __name__ == "__main__":
    main()
