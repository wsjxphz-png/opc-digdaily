#!/usr/bin/env python3
"""
日报正文归档 —— 单元 + 全链路接线测试（不碰网络）

背景：日报正文当天生成、推群、然后丢弃；机会库只存主题名+指纹。2026-09-17 用户
要做月刊才发现月底无料可用。archive_store 把「实际送达」的正文落盘到
storage/archive/YYYY-MM.jsonl，供月刊/复盘使用。

本测试锁死：
  1. 写入格式与字段完整（月刊深加工要靠这些字段）
  2. 按 (date, url/handle) 去重 —— CI 重试重投不写重复行
  3. 空输入不建文件
  4. **全链路接线**：bot.run() 推送成功后会真的写出归档文件（防止「代码写了但没接上」）

运行： cd opc-digdaily && python tests/test_archive.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_TMP_STORAGE = Path(tempfile.mkdtemp(prefix="opc_test_storage_"))
os.environ.setdefault("OPC_STORAGE_DIR", str(_TMP_STORAGE))
# 构造 bot 时会校验 webhook（拒绝未展开的 ${FEISHU_WEBHOOK_URL} 占位符）
os.environ.setdefault("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test-offline")

from archive_store import DailyArchive, opportunity_record, teardown_record  # noqa: E402
from sources.base import ContentItem  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


def mk_item(i=1):
    it = ContentItem(title=f"某人为餐厅做点评代运营", url=f"http://example.com/a/{i}",
                     summary="原始摘要", source_name="r/opps")
    it.translation = "译"
    it.ai_summary = "AI 摘要正文"
    it.opportunity_hint = "卖什么×去哪卖×怎么找顾客"
    it.practical_steps = "1. 交付物：xxx\n2. 前5个客户：yyy\n3. 工具链：zzz"
    it.copy_template = {"who": "餐厅老板", "what": "点评代运营", "first_step": "扫街",
                        "first_prompt": "帮我写开场白", "cost": "0 元两周"}
    it.difficulty = "零门槛"
    it.startup_index = 8
    it.authenticity = 4
    it.code_dependency = 1
    it.topic_key = f"t{i:04d}"
    return it


def unit_tests():
    print("\n=== 1. 归档写入与字段完整性 ===")
    d = Path(tempfile.mkdtemp(prefix="opc_arch_"))
    arc = DailyArchive(d)
    n = arc.append("2026-09-17", "opportunity", [opportunity_record(mk_item(1), "2026-09-17", "domestic")])
    check("写入 1 条", n == 1, f"got {n}")
    f = d / "2026-09.jsonl"
    check("按月建文件 2026-09.jsonl", f.exists())
    rec = json.loads(f.read_text(encoding="utf-8").strip())
    for field in ("url", "title", "summary", "opportunity_hint", "practical_steps",
                  "copy_template", "difficulty", "startup_index", "region", "date"):
        check(f"字段 {field} 已归档", field in rec and rec[field] not in ("", None, {}),
              f"{rec.get(field)!r}"[:60] if field in rec else "缺失")

    print("\n=== 2. 去重（CI 重试重投不写重复行） ===")
    again = arc.append("2026-09-17", "opportunity",
                       [opportunity_record(mk_item(1), "2026-09-17", "domestic")])
    check("同 (date,url) 再写 0 条", again == 0, f"got {again}")
    check("文件仍只有 1 行", len(f.read_text(encoding="utf-8").strip().splitlines()) == 1)
    n2 = arc.append("2026-09-17", "opportunity",
                    [opportunity_record(mk_item(2), "2026-09-17", "international")])
    check("另一条 url 可正常写入", n2 == 1, f"got {n2}")

    print("\n=== 3. 空输入不建文件 ===")
    d2 = Path(tempfile.mkdtemp(prefix="opc_arch_"))
    arc2 = DailyArchive(d2)
    check("空列表返回 0", arc2.append("2026-09-17", "opportunity", []) == 0)
    check("空列表不建文件", not (d2 / "2026-09.jsonl").exists())

    print("\n=== 4. 拆解记录 ===")
    td = {"operator_handle": "@someone", "who": "某人", "deliverable": "交付物",
          "acquisition": "获客", "replicability": 4, "content": "应被剔除的超长字段"}
    n3 = arc.append("2026-09-17", "teardown", [teardown_record(td, "2026-09-17")])
    check("拆解写入 1 条", n3 == 1)
    recs = [json.loads(x) for x in f.read_text(encoding="utf-8").strip().splitlines()]
    td_rec = [r for r in recs if r["module"] == "teardown"][0]
    check("拆解字段保留", td_rec.get("deliverable") == "交付物")
    check("冗余 content 字段被剔除", "content" not in td_rec)
    check("机会与拆解同文件不互相去重", len(recs) == 3, f"共 {len(recs)} 行")

    print("\n=== 5. 跨月分文件 ===")
    arc.append("2026-10-01", "opportunity",
               [opportunity_record(mk_item(9), "2026-10-01", "domestic")])
    check("10 月写进 2026-10.jsonl", (d / "2026-10.jsonl").exists())


async def integration_test():
    """全链路：bot.run() 走完推送成功后，归档必须真的落盘。"""
    print("\n=== 6. 全链路接线（bot.run → 归档落盘）===")
    import main as main_mod
    from test_pipeline import FakeAI  # 复用已有离线桩
    from operators import Operator

    cfg = main_mod.load_config()
    bot = main_mod.DailyOpportunityBot(cfg)
    fake_ai = FakeAI(FakeAI.smart_responder)
    bot.ai = fake_ai
    bot.teardown_engine.ai = fake_ai
    bot.discovery_engine.ai = fake_ai
    bot.opportunity_engine.ai = fake_ai

    def fake_apply_seeds(roster, seeds_path):
        for op in roster.operators.values():
            if "discovery" not in op.sources:
                op.established = True
        roster.operators["zz_test"] = Operator(
            "zz_test", "测试非技术", "国内", ["zz_test"], ["twitter"],
            tech_barrier="无", established=False)
        return {}
    main_mod.apply_seeds = fake_apply_seeds

    async def fake_collect(label, cfg2, history):
        p = "i" if label == "国际" else "d"
        return [
            ContentItem(title="Nick 新流程", url=f"http://{p}/run1",
                        summary="他分享接单", source_name="@nicksaraev"),
            ContentItem(title="某人帮诊所搭建自动化流程", url=f"http://{p}/run2",
                        summary="详细讲了具体交付过程、收费方式和获客渠道",
                        source_name="r/discoverysub"),
        ]
    main_mod._collect = fake_collect

    calls = {"teardown": 0, "opp": 0}

    async def fake_push(teardowns, discovered, date_str):
        calls["teardown"] += 1
        return True

    async def fake_push_opps(domestic, international, date_str, recurring=None,
                             screened_out=0, screened_total=0):
        calls["opp"] += 1
        # 复刻真实 pusher 的契约：把实际送达的 url 记进 _delivered_urls
        bot.pusher._delivered_urls = {
            it.url for it in (domestic + international) if getattr(it, "url", "")
        }
        return True

    bot.pusher.push_teardowns = fake_push
    bot.pusher.push_opportunities = fake_push_opps

    arch_dir = bot.archive.root
    with tempfile.TemporaryDirectory() as td_dir:
        main_mod.ROSTER_PATH = Path(td_dir) / "operators.json"
        try:
            await bot.run()
        finally:
            main_mod.ROSTER_PATH = main_mod.ROSTER_PATH

    files = sorted(arch_dir.glob("*.jsonl")) if arch_dir.exists() else []
    check("bot.run 后产生了归档文件", bool(files), f"{[p.name for p in files]}")
    if not files:
        return
    recs = [json.loads(x) for p in files for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    mods = {r["module"] for r in recs}
    check("归档含机会记录", "opportunity" in mods, f"modules={mods}")
    check("归档含拆解记录", "teardown" in mods, f"modules={mods}")
    opp = [r for r in recs if r["module"] == "opportunity"]
    check("机会归档带正文（摘要/步骤）",
          bool(opp) and all(r.get("summary") or r.get("practical_steps") for r in opp),
          f"{len(opp)} 条")
    check("只归档实际送达的条目",
          all(r.get("url", "").startswith("http") for r in opp), f"{len(opp)} 条")

    print("\n=== 7. 同日不重推（双推闸门） ===")
    # 2026-09-12 实测：看门狗 01:07 补推成功后，当天 13:38 定时 run 又推了一遍同一批
    # 拆解卡。根因是定时 run 不看 push_marker。这里锁死「今天已完整送达 → 整轮跳过」。
    from main import PUSH_MARKER_PATH
    from datetime import datetime, timezone, timedelta
    today_cst = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    marker_ok = PUSH_MARKER_PATH.exists() and PUSH_MARKER_PATH.read_text(encoding="utf-8").strip() == today_cst
    check("首轮跑完已写入今日 marker", marker_ok,
          PUSH_MARKER_PATH.read_text(encoding="utf-8").strip() if PUSH_MARKER_PATH.exists() else "无文件")
    before = dict(calls)
    ok2 = await bot.run()
    check("同日再跑 → 返回 True（不算失败）", ok2 is True)
    check("同日再跑 → 没有二次推送",
          calls == before, f"推送被再调 {calls['teardown']-before['teardown']} 次拆解 / {calls['opp']-before['opp']} 次机会")


def run():
    try:
        unit_tests()
        asyncio.run(integration_test())
    except Exception as e:
        import traceback
        traceback.print_exc()
        RESULTS.append(("未捕获异常", False, str(e)))
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
    run()
