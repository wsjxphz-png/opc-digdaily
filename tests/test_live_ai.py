#!/usr/bin/env python3
"""实时大模型验证（真实 API，不触发飞书推送）。

验证 TeardownEngine / DiscoveryEngine 在真实模型下的返回是否可被正确解析。
运行：cd daily-opportunity-bot && .venv/Scripts/python tests/test_live_ai.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as main_mod
from ai import AIProcessor
from operators import OperatorRoster
from teardown import TeardownEngine
from discovery import DiscoveryEngine
from opportunity import OpportunityEngine, _is_real_opportunity
from sources.base import ContentItem


async def run():
    cfg = main_mod.load_config()
    ai = AIProcessor(
        cfg["ai"]["api_base"], cfg["ai"]["api_key"], cfg["ai"]["model"],
    )
    print("AI enabled:", ai.enabled, "| model:", cfg["ai"]["model"])

    roster = OperatorRoster.build_from_config(cfg, main_mod.ROSTER_PATH)
    main_mod.apply_seeds(roster, main_mod.SEEDS_PATH)
    print(f"名单: {roster.stats()}")

    # ---- 真实拆解：挑一个非技术的操盘手（半佛仙人）----
    op = roster.operators.get("banfo") or next(
        o for o in roster.operators.values()
        if o.tech_barrier == "无" and o.seeded_facts
    )
    print(f"\n=== 真实拆解（非技术）：{op.name} ===")
    td = await TeardownEngine(ai).synthesize(op)
    if td is None:
        print("⚠️ 实时拆解返回 None（模型无返回/解析失败）。请检查 API key / 端点。")
    else:
        for k in ("who", "deliverable", "business_model", "acquisition",
                  "stack", "first_step", "red_flag", "learn"):
            print(f"  {k}: {getattr(td, k)}")
        print(f"  可复制性: {td.replicability}/5")
        print(f"  技术门槛: {td.tech_barrier}  |  你能否照做: {td.doable}")

    # ---- 真实发现 A：技术重的内容 → 应被排除（不返回或标高）----
    item_tech = ContentItem(
        title="这个独立开发者靠写代码做了一款 SaaS，月入 2 万美金",
        url="https://example.com/disc-tech",
        summary="他具体讲了怎么用 React 和后端搭产品、怎么上架应用商店，不是卖课。",
        source_name="r/automation",
    )
    print("\n=== 真实发现 A（技术重内容，应排除）===")
    disc_tech = await DiscoveryEngine(ai, max_scan=10).scan([item_tech], roster)
    kept = [d for d in disc_tech if d.tech_barrier in ("无", "低")]
    print(f"  模型原始返回 {len(disc_tech)} 个；其中非技术(无/低) {len(kept)} 个")
    for d in disc_tech:
        print(f"  🆕 {d.name} | tech_barrier={d.tech_barrier} — {d.highlight}")

    # ---- 真实发现 B：非技术内容 → 应被识别为 无/低 ----
    item_notech = ContentItem(
        title="这个博主靠在 B站做商业解读视频接品牌广告，单条报价几十万",
        url="https://example.com/disc-notech",
        summary="她讲了怎么靠内容涨粉、怎么谈品牌合作、怎么建私域，不是写代码。",
        source_name="r/creator",
    )
    print("\n=== 真实发现 B（非技术内容，应识别）===")
    disc_notech = await DiscoveryEngine(ai, max_scan=10).scan([item_notech], roster)
    print(f"  模型返回 {len(disc_notech)} 个")
    for d in disc_notech:
        print(f"  🆕 {d.name} ({d.region}) | tech_barrier={d.tech_barrier} — {d.highlight}")

    # ---- 模块2 真实机会挖掘：混合真假内容，验证硬过滤 ----
    domestic = [
        ContentItem(
            title="有人靠整理全国宠物友好餐厅清单，在小程序卖年度会员",
            url="https://example.com/opp-d1",
            summary="她用半年时间人工搜集了全国 2000 家宠物友好餐厅，做成可筛选的小程序，年费 99 元，靠小红书引流，已 3000 付费会员。完整讲了怎么找第一批餐厅、怎么谈合作。",
            source_name="r/sidehustle",
        ),
        ContentItem(
            title="我靠做一人公司月入10万，点这里报名我的训练营",
            url="https://example.com/opp-d2",
            summary="文章只讲了我有多厉害、月入多少，结尾让你扫码进群买 1999 元课，没讲任何具体操作。",
            source_name="r/chanel",
        ),
    ]
    international = [
        ContentItem(
            title="I built a directory of remote-friendly companies and sell subscriptions",
            url="https://example.com/opp-i1",
            summary="I manually compiled 3000 companies that allow remote work, put them in a searchable site, charge $49/year. First 50 customers came from a cold post on Reddit and a newsletter. Full breakdown of costs and acquisition.",
            source_name="r/indie",
        ),
        ContentItem(
            title="Join my course to make money online fast",
            url="https://example.com/opp-i2",
            summary="Limited seats! Learn how I made six figures. Buy now before price goes up. No concrete method shared.",
            source_name="r/mmo",
        ),
    ]
    print("\n=== 模块2 真实机会挖掘（应保留真机会、剔除卖铲子）===")
    dom_opps, intl_opps = await OpportunityEngine(ai).mine(domestic, international, per_region=5)
    print(f"  国内保留 {len(dom_opps)} / 国际保留 {len(intl_opps)}")
    for it in dom_opps + intl_opps:
        print(f"  💡 {it.title[:40]} | verdict={it.verdict} auth={it.authenticity} code={it.code_dependency} score={it.relevance_score:.2f}")
    shovel_kept = [it for it in dom_opps + intl_opps if not _is_real_opportunity(it)]
    print(f"  ⚠️ 混入的卖铲子数量: {len(shovel_kept)}（应为 0）")


if __name__ == "__main__":
    asyncio.run(run())
