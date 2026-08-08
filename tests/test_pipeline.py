#!/usr/bin/env python3
"""
操盘手拆解系统 — 端到端测试（无需真实 AI key / 不碰网络）

用 FakeAI 替身模拟大模型返回，覆盖：
  - 拆解引擎：正常 JSON / 数组包裹 / 多出字段 / 非法 JSON 四种情况
  - 发现引擎：正常数组 / 非法 JSON
  - 名单：build_from_config / accumulate 匹配 / 轮转排序
  - 飞书渲染：卡片组装（含禁用态）
  - 完整 run：采集(桩) + 发现 + 归档 + 拆解 + 推送(桩) 全链路

运行： cd daily-opportunity-bot && .venv/Scripts/python tests/test_pipeline.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from operators import Operator, OperatorRoster
from teardown import TeardownEngine
from discovery import DiscoveryEngine, DiscoveredOperator, DISCOVERY_SYSTEM_PROMPT
from main import DailyOpportunityBot, load_config, ROSTER_PATH, _collect
from opportunity import OpportunityEngine, _is_real_opportunity
from sources.base import ContentItem
from sources.weixin_whitelist import WeixinWhitelistSource
from sources.weixin_search import WeixinSearchSource
from sources.source_scout import SourceScout, ScoutedSource
from push import FeishuPusher
import main as main_mod
from filters import is_technical

# ── 样本大模型输出 ──────────────────────────────────────────
TEARDOWN_JSON = json.dumps({
    "who": "测试人物：一个真实交付型操盘手",
    "deliverable": "帮企业搭自动化流程",
    "business_model": "项目费 $2K–10K + 月维护 $1K",
    "acquisition": "冷邮件 + YouTube 公开构建",
    "stack": "n8n + Stripe",
    "replicability": 4,
    "first_step": "今天用 n8n 搭一个自动流程",
    "red_flag": "不是卖课党",
    "learn": "选一个具体行业痛点",
}, ensure_ascii=False)

# 多出字段的版本（用于验证不会崩溃）
TEARDOWN_JSON_EXTRA = json.dumps({
    "who": "测试人物", "deliverable": "X", "business_model": "Y",
    "acquisition": "Z", "stack": "W", "replicability": 3,
    "first_step": "A", "red_flag": "B", "learn": "C",
    "summary": "模型多返回的杂项字段", "score": 0.9,  # ← 额外键，应被忽略
}, ensure_ascii=False)

TEARDOWN_JSON_ARRAY = "[" + TEARDOWN_JSON + "]"  # 被数组包裹

# 含技术门槛 + 你能否照做 字段的版本
TEARDOWN_JSON_TB = json.dumps({
    "who": "内容创作者：不做软件", "deliverable": "newsletter + 课",
    "business_model": "订阅+卖课", "acquisition": "社媒日更",
    "stack": "newsletter 工具", "replicability": 5,
    "first_step": "开 newsletter", "red_flag": "非卖铲子", "learn": "持续输出",
    "tech_barrier": "无", "doable": "能",
}, ensure_ascii=False)

DISCOVERY_JSON = json.dumps([{
    "index": 0, "is_operator": True, "name": "张三", "handle": "@zhangsan",
    "region": "国内", "highlight": "帮律所搭自动化流程", "reason": "内容讲他具体交付",
}], ensure_ascii=False)

# 模型有时直接返回单个对象而非数组（实时模型实测会这样）
DISCOVERY_JSON_OBJ = json.dumps({
    "index": 0, "is_operator": True, "name": "张三", "handle": "@zhangsan",
    "region": "国内", "highlight": "帮律所搭自动化流程", "reason": "内容讲他具体交付",
}, ensure_ascii=False)


class FakeAI:
    """模拟 AIProcessor.call_llm。responder: 字符串(固定返回) 或 callable(sys,user)->str"""
    def __init__(self, responder):
        self.enabled = True
        self.responder = responder
        self.calls = []

    async def call_llm(self, system_prompt, user_content, max_tokens=2000, temperature=0.3):
        self.calls.append((system_prompt, user_content))
        if callable(self.responder):
            return self.responder(system_prompt, user_content)
        return self.responder if isinstance(self.responder, str) else ""

    @staticmethod
    def smart_responder(system_prompt, user_content):
        # 根据系统提示区分拆解 / 发现
        if "商业拆解教练" in system_prompt:
            return TEARDOWN_JSON
        if "新机会猎手" in system_prompt or "真实赚钱个人" in system_prompt:
            return DISCOVERY_JSON
        return ""

    async def process(self, items):
        """模拟 AIProcessor.process：偶数条标为「真机会」，奇数条标为「卖铲子」。"""
        out = []
        for i, it in enumerate(items):
            if i % 2 == 0:
                it.ai_processed = True
                it.verdict = "可复刻的真机会"
                it.authenticity = 4
                it.code_dependency = 2
                it.relevance_score = 0.8
                it.ai_summary = "一个可复刻的小生意，无需写代码。"
                it.opportunity_hint = "卖整理好的信息差，去社群找顾客，收会员费。"
                it.practical_steps = "1.交付物：一份行业清单\n2.前5个客户：冷邮件\n3.工具链：Notion+表单"
            else:
                it.ai_processed = True
                it.verdict = "卖噱头/卖铲子"
                it.authenticity = 1
                it.code_dependency = 5
                it.relevance_score = 0.1
            out.append(it)
        return out


# ── 测试框架 ────────────────────────────────────────────────
RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


async def main_tests():
    print("\n=== 1. 拆解引擎 ===")
    op = Operator("@@t", "测试", "国内", ["@@t"], ["twitter"])

    # 1.1 正常 JSON
    ai = FakeAI(TEARDOWN_JSON)
    td = await TeardownEngine(ai).synthesize(op)
    check("正常 JSON → 返回拆解卡", td is not None)
    check("字段正确解析", td is not None and td.who.startswith("测试人物") and td.replicability == 4)
    check("回写 dossier(teardown_count++)", op.teardown_count == 1 and op.teardown is not None)

    # 1.2 数组包裹（曾经的崩溃路径）
    op2 = Operator("@@arr", "数组", "国际", ["@@arr"], ["twitter"])
    ai = FakeAI(TEARDOWN_JSON_ARRAY)
    td2 = await TeardownEngine(ai).synthesize(op2)
    check("数组包裹 JSON → 不崩溃且返回", td2 is not None, "此前会因 **data 崩溃")
    check("数组包裹字段正确", td2 is not None and td2.replicability == 4)

    # 1.3 多出字段（曾经的崩溃路径）
    op3 = Operator("@@ex", "多余", "国内", ["@@ex"], ["twitter"])
    ai = FakeAI(TEARDOWN_JSON_EXTRA)
    td3 = await TeardownEngine(ai).synthesize(op3)
    check("多出字段 JSON → 不崩溃且忽略杂项", td3 is not None and td3.who == "测试人物")
    check("replicability 强制为 int", td3 is not None and isinstance(td3.replicability, int))

    # 1.4 非法 JSON
    op4 = Operator("@@bad", "坏", "国际", ["@@bad"], ["twitter"])
    ai = FakeAI("这根本不是 JSON 啊啊啊")
    td4 = await TeardownEngine(ai).synthesize(op4)
    check("非法 JSON → 返回 None 且不崩溃", td4 is None)

    # 1.5 空返回（AI 未启用场景）
    op5 = Operator("@@empty", "空", "国内", ["@@empty"], ["twitter"])
    ai = FakeAI("")
    td5 = await TeardownEngine(ai).synthesize(op5)
    check("空返回 → 返回 None", td5 is None)

    # 1.6 技术门槛 + 你能否照做 字段
    op6 = Operator("@@tb", "门槛", "国内", ["@@tb"], ["twitter"])
    ai = FakeAI(TEARDOWN_JSON_TB)
    td6 = await TeardownEngine(ai).synthesize(op6)
    check("tech_barrier 字段解析", td6 is not None and td6.tech_barrier == "无")
    check("doable 字段解析", td6 is not None and td6.doable == "能")

    print("\n=== 2. 发现引擎 ===")
    items = [ContentItem(title="某人帮律所搭流程做自动化交付", url="http://x/1",
                         summary="内容详细讲了他具体怎么交付、收多少钱、客户从哪来",
                         source_name="r/testsub")]
    # 2.1 正常数组
    ai = FakeAI(DISCOVERY_JSON)
    empty_roster = OperatorRoster(ROOT / "storage" / "_nonexistent.json")
    disc = await DiscoveryEngine(ai, max_scan=30).scan(items, empty_roster)
    check("正常数组 → 返回 1 个发现", len(disc) == 1, f"got {len(disc)}")
    check("发现对象字段正确", disc and disc[0].name == "张三" and disc[0].handle == "@zhangsan")

    # 2.2 commit 去重
    committed = await disc[0].commit(empty_roster)
    committed2 = await disc[0].commit(empty_roster)
    check("首次 commit 成功", committed is True)
    check("重复 commit 被去重", committed2 is False)
    check("已写入空 roster", "@zhangsan" in empty_roster.operators)

    # 2.3 非法 JSON
    ai = FakeAI("不是 JSON")
    disc_bad = await DiscoveryEngine(ai, max_scan=30).scan(items, empty_roster)
    check("非法 JSON → 返回空列表不崩溃", disc_bad == [])

    # 2.4 单对象返回（模型不包数组，实时模型实测会这样）
    ai = FakeAI(DISCOVERY_JSON_OBJ)
    disc_obj = await DiscoveryEngine(ai, max_scan=30).scan(items, empty_roster)
    check("单对象返回 → 正确解析为 1 个发现", len(disc_obj) == 1, f"got {len(disc_obj)}")

    # 2.5 技术门槛过滤：中/高 的发现应被排除（保护不会写代码的用户）
    high_op = DiscoveredOperator(name="码农", handle="@coder", region="国内",
                                 highlight="卖代码模板", reason="r", tech_barrier="高")
    check("技术门槛=高 的发现被排除(commit False)",
          (await high_op.commit(empty_roster)) is False)
    low_op = DiscoveredOperator(name="写作者", handle="@writer", region="国内",
                                highlight="卖写作课", reason="r", tech_barrier="低")
    check("技术门槛=低 的发现可入名单(commit True)",
          (await low_op.commit(empty_roster)) is True)

    print("\n=== 3. 名单：构建 / 归档 / 轮转 ===")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td_dir:
        tmp = Path(td_dir) / "operators.json"
        roster = OperatorRoster.build_from_config(cfg, tmp)
        st = roster.stats()
        check("build_from_config 构建出名单(>50)", st["total"] > 50, f"total={st['total']}")
        check("地区分布含国内+国际", "国内" in st["by_region"] and "国际" in st["by_region"])
        check("build 后磁盘文件已生成", tmp.exists())

        # 归档匹配：用真实存在的 handle @nicksaraev
        it = ContentItem(title="Nick 的新工作流", url="http://n/1",
                         summary="他分享了接单方法", source_name="@nicksaraev")
        matched = roster.accumulate(it)
        check("source_name 命中 → 归档成功", matched is True)
        nicks = roster.operators["@nicksaraev"]
        check("信号已写入该操盘手", len(nicks.signals) == 1)
        # 重复 url 不重复计数
        roster.accumulate(it)
        check("同 url 不重复归档", len(nicks.signals) == 1)
        # 未命中
        miss = roster.accumulate(ContentItem(title="t", url="http://m/1",
                                             summary="s", source_name="r/unknown999"))
        check("未知 source_name → 不归档", miss is False)

        # 轮转：先挑一个待拆解
        due1 = roster.get_due_for_teardown(1, require_signals=False)
        check("轮转返回 1 人", len(due1) == 1)
        op_due = due1[0]

        # 信号优先：有信号者应排在无信号前面（在标记“当天已拆解”之前验证）
        due_all = roster.get_due_for_teardown(999, require_signals=False)
        check("排序：有信号者优先(排在最前)",
              due_all[0].handle == "@nicksaraev",
              f"due[0]={due_all[0].handle}")

        # 标记“当天已拆解” → 应被轮转跳过（get_due_for_teardown 会过滤 today）
        op_due.last_teardown = "2026-08-08"
        due2 = roster.get_due_for_teardown(1, require_signals=False)
        check("当天已拆解者被轮转跳过",
              op_due.handle not in [o.handle for o in due2] or len(due2) == 0)

    print("\n=== 4. 飞书卡片渲染 ===")
    # 4.1 禁用态（空 webhook）
    dis = FeishuPusher(webhook_url="", card_color="blue")
    ok_dis = await dis.push_teardowns([], [], "2026年08月08日")
    check("空 webhook → 不发送(返回 False)", ok_dis is False)

    # 4.2 正常渲染（桩掉真实网络发送）
    pusher = FeishuPusher(webhook_url="https://open.feishu.cn/robottest", card_color="blue")
    captured = {}
    async def fake_send(card):
        captured["card"] = card
        return True
    pusher._send_card = fake_send
    sample_td = {
        "operator_name": "Nick 测试", "region": "国际", "who": "人物",
        "deliverable": "交付", "business_model": "模式", "acquisition": "获客",
        "stack": "工具", "first_step": "第一步", "red_flag": "风险",
        "learn": "学习点", "replicability": 4, "tech_barrier": "无",
        "doable": "能", "signals_used": 2,
    }
    sample_disc = {"name": "张三", "handle": "@zhangsan", "region": "国内",
                   "highlight": "帮律所搭流程"}
    ok = await pusher.push_teardowns([sample_td], [sample_disc], "2026年08月08日")
    check("推送返回 True", ok is True)
    card = captured.get("card", {})
    check("卡片含 header", "header" in card and "title" in card.get("header", {}))
    check("卡片含拆解段(>5 元素)", len(card.get("elements", [])) > 5)
    # 直接测构建方法
    sec = pusher._build_teardown_section(sample_td)
    check("_build_teardown_section 产出字段", any("商业模式" in e["text"]["content"] for e in sec))
    check("_build_teardown_section 含技术门槛", any("技术门槛" in e["text"]["content"] for e in sec))
    check("_build_teardown_section 含你能否照做", any("你能否照做" in e["text"]["content"] for e in sec))
    alert = pusher._build_discovery_alert(sample_disc)
    check("_build_discovery_alert 含新发现", "发现新操盘手" in alert[0]["text"]["content"])

    print("\n=== 5. 完整 run() 全链路（桩采集+桩推送）===")
    config = load_config()
    bot = DailyOpportunityBot(config)
    fake_ai = FakeAI(FakeAI.smart_responder)
    bot.ai = fake_ai
    # __init__ 里引擎已绑定旧 ai，必须同步替换，否则离线测试会真的打 API
    bot.teardown_engine.ai = fake_ai
    bot.discovery_engine.ai = fake_ai
    bot.opportunity_engine.ai = fake_ai

    # 桩掉 apply_seeds：模拟真实行为——配置里的成名盯人列表标记为 established=True（不推送），
    # 并注入一个「新的非技术」测试操盘手（established=False，应被推送）
    def fake_apply_seeds(roster, seeds_path):
        for op in roster.operators.values():
            if "discovery" not in op.sources:
                op.established = True
        roster.operators["zz_test"] = Operator(
            "zz_test", "测试非技术", "国内", ["zz_test"], ["twitter"],
            tech_barrier="无", established=False)
        return {}
    main_mod.apply_seeds = fake_apply_seeds

    # 桩掉采集：返回一条命中归档 + 一条进入发现
    async def fake_collect(label, cfg2, history):
        return [
            ContentItem(title="Nick 新流程", url="http://n/run1",
                        summary="他分享接单", source_name="@nicksaraev"),
            ContentItem(title="某人帮诊所搭建自动化流程", url="http://d/run1",
                        summary="详细讲了具体交付过程、收费方式和获客渠道",
                        source_name="r/discoverysub"),
        ]
    main_mod._collect = fake_collect

    captured_push = {}
    async def fake_push(teardowns, discovered, date_str):
        captured_push["t"] = teardowns
        captured_push["d"] = discovered
        captured_push["date"] = date_str
        return True
    bot.pusher.push_teardowns = fake_push

    captured_opp = {}
    async def fake_push_opps(domestic, international, date_str):
        captured_opp["dom"] = domestic
        captured_opp["intl"] = international
        captured_opp["date"] = date_str
        return True
    bot.pusher.push_opportunities = fake_push_opps

    # 用临时名单，避免污染真实 storage/operators.json
    with tempfile.TemporaryDirectory() as td_dir:
        main_mod.ROSTER_PATH = Path(td_dir) / "operators.json"
        try:
            await bot.run()
        finally:
            main_mod.ROSTER_PATH = ROSTER_PATH

    check("run 产出至少 1 张拆解卡", len(captured_push.get("t", [])) >= 1,
          f"got {len(captured_push.get('t', []))}")
    check("run 触发发现引擎", len(captured_push.get("d", [])) >= 1,
          f"discovered={len(captured_push.get('d', []))}")
    check("推送被调用且带日期", bool(captured_push.get("date")))
    # 模块2 也应在全链路中被执行并推送
    check("模块2(run) 产出国内机会", len(captured_opp.get("dom", [])) >= 1,
          f"dom={len(captured_opp.get('dom', []))}")
    check("模块2(run) 产出国际机会", len(captured_opp.get("intl", [])) >= 1,
          f"intl={len(captured_opp.get('intl', []))}")
    check("模块2 自动剔除卖铲子(数量<=采集数)",
          len(captured_opp.get("dom", [])) + len(captured_opp.get("intl", [])) <= 4,
          f"total={len(captured_opp.get('dom', [])) + len(captured_opp.get('intl', []))}")
    if captured_push.get("t"):
        t0 = captured_push["t"][0]
        check("拆解卡字段完整(含 replicability)", "replicability" in t0 and "who" in t0)

    # 聚焦「机会」：已成名大V（如配置里的 @nicksaraev）不应被推送，只推新/非成名的人
    pushed_handles = [t.get("operator_handle") for t in captured_push.get("t", [])]
    check("run 不推送已成名大V(@nicksaraev)", "@nicksaraev" not in pushed_handles,
          f"pushed={pushed_handles}")

    print("\n=== 6. 技术门槛过滤（非技术用户保护）===")
    r2 = OperatorRoster(ROOT / "storage" / "_nonexistent6.json")
    r2.operators["a"] = Operator("a", "高门槛", "国内", ["a"], ["twitter"], tech_barrier="高")
    r2.operators["b"] = Operator("b", "低门槛", "国内", ["b"], ["twitter"], tech_barrier="低")
    r2.operators["c"] = Operator("c", "无门槛", "国内", ["c"], ["twitter"], tech_barrier="无")
    due = r2.get_due_for_teardown(10, allowed_tech_barrier=["无", "低"])
    due_handles = [o.handle for o in due]
    check("轮转过滤只保留 无/低（排除需写代码）", set(due_handles) == {"b", "c"}, f"got {due_handles}")
    due_all = r2.get_due_for_teardown(10)
    check("不传 allowed → 不做技术过滤(含高门槛)", "a" in [o.handle for o in due_all])

    print("\n=== 6.5 聚焦机会：排除已成名大V ===")
    r3 = OperatorRoster(ROOT / "storage" / "_nonexistent65.json")
    # 成名多年的大V（established=True）→ 不是机会，不推送
    r3.operators["famous"] = Operator("famous", "半佛仙人", "国内", ["famous"], ["rss"],
                                       tech_barrier="无", established=True)
    # 刚冒头的新人（established=False）→ 机会，应推送
    r3.operators["newbie"] = Operator("newbie", "新晋小白", "国内", ["newbie"], ["discovery"],
                                      tech_barrier="无", established=False)
    due_e = r3.get_due_for_teardown(10, exclude_established=True)
    due_e_h = [o.handle for o in due_e]
    check("轮转排除已成名(established=True)", "famous" not in due_e_h, f"got {due_e_h}")
    check("轮转保留新晋(established=False)", "newbie" in due_e_h, f"got {due_e_h}")
    due_no_e = r3.get_due_for_teardown(10, exclude_established=False)
    check("exclude_established=False 时成名者也纳入", "famous" in [o.handle for o in due_no_e])

    # 发现引擎：已成名大V 的发现应被 commit 排除
    famous_disc = DiscoveredOperator(name="老牌大V", handle="@oldvip", region="国内",
                                     highlight="红了十年", reason="r",
                                     tech_barrier="无", established=True)
    check("发现 established=True(成名大V) → commit False",
          (await famous_disc.commit(r3)) is False)
    new_disc = DiscoveredOperator(name="刚起步的新人", handle="@fresh", region="国内",
                                  highlight="刚跑通一人模式", reason="r",
                                  tech_barrier="无", established=False)
    check("发现 established=False(新机会) → commit True",
          (await new_disc.commit(r3)) is True)

    print("\n=== 7. 模块2 赚钱机会挖掘 ===")
    def mk_item(idx, verdict, auth, code, score, title="机会标题"):
        it = ContentItem(title=title, url=f"http://x/{idx}", summary="s", source_name="r/opps")
        it.ai_processed = True
        it.verdict = verdict
        it.authenticity = auth
        it.code_dependency = code
        it.relevance_score = score
        return it

    # 7.1 硬过滤：四类卖铲子/不可做的一律剔除
    shovel = mk_item(1, "卖噱头/卖铲子", 1, 5, 0.05)      # 卖铲子
    low_auth = mk_item(2, "可复刻的真机会", 2, 2, 0.5)      # 真实性<3
    high_code = mk_item(3, "可复刻的真机会", 4, 4, 0.8)     # 代码依赖>=4
    low_score = mk_item(4, "可复刻的真机会", 4, 2, 0.3)     # 综合分<0.4
    real = mk_item(5, "可复刻的真机会", 5, 1, 0.9)          # 真机会
    for label, it in [("卖铲子", shovel), ("真实性低", low_auth),
                      ("需写代码", high_code), ("综合分低", low_score)]:
        check(f"剔除{label}", not _is_real_opportunity(it))
    check("保留真机会", _is_real_opportunity(real))

    # 7.2 _select 平衡取前 N + 排序
    items = [real, mk_item(6, "可复刻的真机会", 4, 2, 0.6),
             mk_item(7, "可复刻的真机会", 4, 2, 0.7), shovel, low_auth, high_code]
    sel = OpportunityEngine._select(items, 5)
    check("_select 只留真机会", all(_is_real_opportunity(i) for i in sel))
    check("_select 按分数降序", [i.relevance_score for i in sel] == sorted(
        [i.relevance_score for i in sel], reverse=True))

    # 7.3 mine 国内/国际各取 5（用全真 FakeAI 验证 cap）
    class FakeAIAllReal(FakeAI):
        async def process(self, items):
            for it in items:
                it.ai_processed = True
                it.verdict = "可复刻的真机会"
                it.authenticity = 5
                it.code_dependency = 1
                it.relevance_score = 0.9
            return items

    eng = OpportunityEngine(FakeAIAllReal("ignored"))
    dom = [mk_item(100 + i, "可复刻的真机会", 5, 1, 0.9, f"国内{i}") for i in range(8)]
    intl = [mk_item(200 + i, "可复刻的真机会", 5, 1, 0.9, f"国际{i}") for i in range(8)]
    d_opps, i_opps = await eng.mine(dom, intl, per_region=5)
    check("mine 国内=capped 5", len(d_opps) == 5, f"got {len(d_opps)}")
    check("mine 国际=capped 5", len(i_opps) == 5, f"got {len(i_opps)}")
    check("mine 只输出真机会", all(_is_real_opportunity(x) for x in d_opps + i_opps))

    # 7.4 推送渲染：国内+国际双板块
    pusher = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/test", "blue")
    cap = {}
    async def fake_card(card):
        cap["c"] = card
        return True
    pusher._send_card = fake_card
    ok_opp = await pusher.push_opportunities(dom[:3], intl[:3], "2026年08月08日")
    check("push_opportunities 返回 True", ok_opp)
    card = cap.get("c", {})
    txt = "\n".join(e.get("text", {}).get("content", "")
                    for e in card.get("elements", []) if e.get("tag") == "div")
    check("卡片含国内板块", "国内机会" in txt)
    check("卡片含国际板块", "国际机会" in txt)
    check("卡片标题为模块2", "赚钱机会" in card.get("header", {}).get("title", {}).get("content", ""))

    # 7.5 国际内容：中文翻译做主标题，英文原标题降级为脚注（能翻译就翻译，少英文）
    it_en = ContentItem(title="I built a directory of remote companies",
                        url="http://x/en", summary="s", source_name="reddit")
    it_en.translation = "我整理了「支持远程办公的公司清单」并卖订阅"
    it_en.ai_processed = True
    it_en.verdict = "可复刻的真机会"
    it_en.authenticity = 5
    it_en.code_dependency = 2
    it_en.relevance_score = 0.9
    cap2 = {}
    async def fake2(c):
        cap2["c"] = c
        return True
    pusher._send_card = fake2
    await pusher.push_opportunities([], [it_en], "2026年08月08日")
    txt2 = "\n".join(e.get("text", {}).get("content", "")
                     for e in cap2["c"].get("elements", []) if e.get("tag") == "div")
    check("国际卡主标题用中文翻译", "我整理了「支持远程办公的公司清单」并卖订阅" in txt2)
    check("国际卡保留英文原标题脚注", ("I built a directory of remote companies" in txt2 and "英文原标题" in txt2))


    print("\n=== 8. 公众号白名单源（名单制采集） ===")
    wl = WeixinWhitelistSource()
    check("白名单源可实例化(name=weixin-whitelist)",
          wl is not None and wl.name == "weixin-whitelist")
    items_dup = [
        ContentItem(title="A", url="http://a", summary="", source="weixin_whitelist", source_name="x"),
        ContentItem(title="A", url="http://a", summary="", source="weixin_whitelist", source_name="x"),
        ContentItem(title="B", url="http://b", summary="", source="weixin_whitelist", source_name="y"),
    ]
    uniq = WeixinWhitelistSource._dedupe(items_dup)
    check("白名单 _dedupe 去重(3→2)", len(uniq) == 2, f"got {len(uniq)}")
    # 未启用 / 未配置账号 → 优雅返回空
    empty_cfg = await wl.fetch({"enabled": False}, {})
    check("未启用 → 返回空列表不崩溃", empty_cfg == [])
    empty_cfg2 = await wl.fetch({"enabled": True, "accounts": []}, {})
    check("未配置账号 → 返回空列表不崩溃", empty_cfg2 == [])

    print("\n=== 9. 复盘更新 + 补拆 轮转逻辑 ===")
    r4 = OperatorRoster(ROOT / "storage" / "_nonexistent8.json")
    # 应被挑出：已拆解 + 非成名 + 超过间隔 + 有新信号
    due_op = Operator("rev1", "老张", "国内", ["rev1"], ["rss"], tech_barrier="无", established=False)
    due_op.teardown_count = 1
    due_op.last_teardown = "2026-07-01"
    due_op.teardown = {"who": "老张", "business_model": "卖课"}
    due_op.signals = [{"date": "2026-08-01", "title": "老张开了新直播间", "url": "u", "summary": "s"}]
    r4.operators["rev1"] = due_op
    # 不应被挑出：已拆解但无新信号（信号早于上次拆解）
    nodue = Operator("rev2", "老李", "国内", ["rev2"], ["rss"], tech_barrier="无", established=False)
    nodue.teardown_count = 1
    nodue.last_teardown = "2026-07-01"
    nodue.teardown = {"who": "老李"}
    nodue.signals = [{"date": "2026-06-01", "title": "旧动态", "url": "u", "summary": "s"}]
    r4.operators["rev2"] = nodue
    # 不应被挑出：从未拆解过（补拆走的是 teardown 轮转，不是复盘）
    never = Operator("rev3", "新人", "国内", ["rev3"], ["discovery"], tech_barrier="无", established=False)
    never.signals = [{"date": "2026-08-05", "title": "x", "url": "u", "summary": "s"}]
    r4.operators["rev3"] = never
    # 不应被挑出：已成名大V
    est = Operator("rev4", "大V", "国内", ["rev4"], ["rss"], tech_barrier="无", established=True)
    est.teardown_count = 5
    est.last_teardown = "2026-01-01"
    est.signals = [{"date": "2026-08-05", "title": "x", "url": "u", "summary": "s"}]
    r4.operators["rev4"] = est

    due_pool = r4.get_due_for_revisit(5, interval_days=14, allowed_tech_barrier=["无", "低"])
    due_h = [o.handle for o in due_pool]
    check("复盘更新只挑「已拆+有新信号+超期+非成名」", due_h == ["rev1"], f"got {due_h}")

    print("\n=== 10. 复盘更新卡合成（is_revisit） ===")
    REVISIT_JSON = json.dumps({
        "who": "老张(更新)", "deliverable": "新开的直播课", "business_model": "直播+会员",
        "acquisition": "私域", "stack": "视频号", "replicability": 4,
        "first_step": "先开一场直播", "red_flag": "别囤货", "learn": "顺势更新",
        "tech_barrier": "无", "doable": "能",
    }, ensure_ascii=False)

    class FakeAIRevisit(FakeAI):
        async def call_llm(self, system_prompt, user_content, max_tokens=2000, temperature=0.3):
            return REVISIT_JSON

    rev_td = await TeardownEngine(FakeAIRevisit("ignored")).synthesize_revisit(due_op, due_op.teardown)
    check("synthesize_revisit 返回复盘卡", rev_td is not None)
    check("复盘卡 is_revisit=True", rev_td is not None and rev_td.is_revisit is True)
    check("复盘卡回写 last_revisit", due_op.last_revisit is not None,
          f"last_revisit={due_op.last_revisit}")
    check("复盘卡 revisit_count++ (=1)", due_op.revisit_count == 1, f"got {due_op.revisit_count}")
    # 复盘卡的飞书渲染应体现「动态更新」标题
    pusher_r = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/testr", "blue")
    rev_section = pusher_r._build_teardown_section(rev_td.to_dict())
    rev_txt = "\n".join(e.get("text", {}).get("content", "")
                        for e in rev_section if e.get("tag") == "div")
    check("复盘卡卡片标题含「动态更新」", "动态更新" in rev_txt, f"txt={rev_txt[:60]}")

    print("\n=== 11. 发现引擎「永远有人值得推荐」框架 ===")
    check("发现提示词坚持「永远能找到人(长期价值)」",
          "永远" in DISCOVERY_SYSTEM_PROMPT and "长期价值" in DISCOVERY_SYSTEM_PROMPT)
    check("发现提示词排除卖铲子类(媒体/写代码/成名大V)",
          "媒体" in DISCOVERY_SYSTEM_PROMPT and "写代码" in DISCOVERY_SYSTEM_PROMPT
          and "大V" in DISCOVERY_SYSTEM_PROMPT)

    print("\n=== 12. 飞书分批推送（不封顶/分批防截断） ===")
    # 构造 10 条机会，batch_size=4 → 应拆成 3 批
    pusher_b = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/testb", "blue", batch_size=4)
    batches = []
    async def fake_b(c):
        batches.append(c)
        return True
    pusher_b._send_card = fake_b
    many = [mk_item(300 + i, "可复刻的真机会", 5, 1, 0.9, f"机会{i}") for i in range(10)]
    ok_b = await pusher_b.push_opportunities(many, [], "2026年08月08日")
    check("分批推送返回 True", ok_b)
    check("10 条按 batch_size=4 拆成 3 批", len(batches) == 3, f"got {len(batches)} 批")
    # 用 URL 中的唯一索引清点每批实际机会条数（避免误算标题/统计文本）
    import re as _re
    all_idx = []
    for b in batches:
        s = json.dumps(b, ensure_ascii=False)
        all_idx.extend(int(x) for x in _re.findall(r"http://x/(\d+)", s))
    per_batch = [_re.findall(r"http://x/(\d+)", json.dumps(b, ensure_ascii=False)) for b in batches]
    check("每批条数 ≤ batch_size(4)", all(len(p) <= 4 for p in per_batch), f"per_batch={per_batch}")
    check("分批覆盖全部 10 条且无丢失", sorted(all_idx) == list(range(300, 310)), f"got {sorted(all_idx)}")


async def scout_tests():
    print("\n=== 13. 内容源侦察兵（SourceScout）===")
    import tempfile
    SCOUT_JSON = json.dumps([
        {"name": "增长黑盒", "platform": "weixin", "verdict": "add",
         "score": 9, "reason": "持续写一人公司实操复盘，有真实交付案例"},
        {"name": "卖课老王", "platform": "weixin", "verdict": "reject",
         "score": 2, "reason": "纯卖课党，收入来自教别人赚钱，不靠真实交付"},
    ], ensure_ascii=False)

    tmpf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
    tmpf.close()
    dyn = Path(tmpf.name)

    scout = SourceScout(dyn, {"min_score": 7, "rejudge_days": 60})
    ai_s = FakeAI(SCOUT_JSON)
    approved = await scout.judge(ai_s, [("增长黑盒", "写一人公司"), ("卖课老王", "卖课")])
    check("judge 通过 1 个(add 且 score≥7)", len(approved) == 1 and approved[0].name == "增长黑盒")
    check("注册表记录 2 条（add+reject）", len(scout._registry["entries"]) == 2)
    check("approved_accounts 只含通过项", scout.approved_accounts() == ["增长黑盒"])
    check("注册表已落盘到动态文件", dyn.exists())

    # 重评估窗口：已知账号在窗口内应跳过
    check("已知账号在窗口内 _is_fresh=True", scout._is_fresh("增长黑盒"))
    check("未知账号 _is_fresh=False", not scout._is_fresh("完全陌生的号"))

    # 解析兼容
    check("空串解析为 []", SourceScout._parse("") == [])
    check("```json 包裹可解析", len(SourceScout._parse("```json\n" + SCOUT_JSON + "\n```")) == 2)

    # scout() 全编排（mock gather，不碰网络）
    async def fake_gather(self, client, items=None):
        return [("增长黑盒", "写一人公司"), ("卖课老王", "卖课")]
    scout.gather_candidates = fake_gather.__get__(scout, SourceScout)
    approved2 = await scout.scout(ai_s, None)
    check("scout() 编排返回通过项", len(approved2) == 1 and approved2[0].name == "增长黑盒")

    print("\n=== 14. 白名单并入动态源注册表 ===")
    import sources.weixin_whitelist as wlm
    wlm._DYNAMIC_FILE = dyn  # 指向刚写好的注册表
    merged = WeixinWhitelistSource._merge_dynamic(["阿猫读书", "findyi"])
    check("并入动态源(增长黑盒)", "增长黑盒" in merged)
    check("静态账号保留(阿猫读书)", "阿猫读书" in merged and "findyi" in merged)
    check("不重复已有账号", merged.count("阿猫读书") == 1)
    check("不并入 reject 项(卖课老王)", "卖课老王" not in merged)

    print("\n=== 15. 飞书「新挖掘内容源」卡片 ===")
    class CapturePusher(FeishuPusher):
        def __init__(self):
            super().__init__("https://open.feishu.cn/open-apis/bot/v2/hook/testx", "purple", 8)
            self.cards = []
        async def _send_card(self, card):
            self.cards.append(card)
            return True
    cp = CapturePusher()
    srcs = [ScoutedSource("增长黑盒", "weixin", "add", 9, "持续写一人公司实操")]
    ok = await cp.push_scouted_sources(srcs, "2026年08月08日")
    check("push_scouted_sources 返回 True", ok)
    check("生成 1 张卡", len(cp.cards) == 1)
    check("卡片含账号名", "增长黑盒" in json.dumps(cp.cards[0], ensure_ascii=False))
    check("卡片标题含「新挖掘内容源」", "新挖掘内容源" in json.dumps(cp.cards[0], ensure_ascii=False))
    check("空源返回 False", not await cp.push_scouted_sources([], "2026年08月08日"))

    dyn.unlink(missing_ok=True)

    print("\n=== 16. 确定性「非技术」硬过滤（用户无代码背景，绝不推技术向案例/源）===")
    # 16.1 关键词识别本身正确
    check("is_technical 命中 技术大牛/程序员/独立开发者",
          is_technical("技术大牛分享代码") and is_technical("前程序员创业") and is_technical("独立开发者做 SaaS"))
    check("is_technical 放行 普通 OPC 案例",
          not is_technical("宝妈靠小红书带货月入3万") and not is_technical("退休教师做知识付费社群"))
    check("is_technical 命中 indie hacker / AI engineer（英文）",
          is_technical("indie hacker shipped a SaaS") and is_technical("AI engineer 训练大模型"))

    # 16.2 模块1：发现引擎即便把 tech_barrier 误判为「无」，也确定性挡掉技术大牛
    tech_op = DiscoveredOperator(
        name="码神老王", handle="@coder_wang",
        region="国内", highlight="前阿里程序员，技术大牛，分享写代码经验",
        reason="他靠技术影响力变现", tech_barrier="无", established=False,
    )
    rtmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    rtmp.close()
    roster_tech = OperatorRoster(rtmp.name)
    added_tech = await tech_op.commit(roster_tech)
    check("发现引擎确定性排除技术大牛（即使 tech_barrier=无）", not added_tech)
    Path(rtmp.name).unlink(missing_ok=True)

    # 16.3 模块2：含技术信号的文章确定性不通过硬过滤
    tech_item = ContentItem(
        title="独立开发者用 Python 写了一个 SaaS", url="http://x", source="rss", source_name="r",
    )
    tech_item.ai_processed = True
    tech_item.verdict = "可复刻的真机会"
    tech_item.authenticity = 5
    tech_item.code_dependency = 2   # AI 可能低判代码依赖，但关键词兜底
    tech_item.relevance_score = 0.9
    check("模块2 确定性排除技术向文章", not _is_real_opportunity(tech_item))
    normal_item = ContentItem(
        title="普通人靠信息差做闲鱼无货源月入过万", url="http://y", source="rss", source_name="r",
    )
    normal_item.ai_processed = True
    normal_item.verdict = "可复刻的真机会"
    normal_item.authenticity = 5
    normal_item.code_dependency = 1
    normal_item.relevance_score = 0.9
    check("模块2 放行普通 OPC 文章", _is_real_opportunity(normal_item))

    # 16.4 侦察兵：技术向内容源确定性判 reject，不进白名单
    sdyn = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    sdyn.close()
    scout = SourceScout(sdyn.name, {"min_score": 7})
    # 直接构造一个被判 add 的技术源，验证 judge 的强制 reject 逻辑
    class _FakeAI:
        async def call_llm(self, *a, **k):
            return json.dumps([{"name": "程序猿日记", "platform": "weixin",
                                "verdict": "add", "score": 9,
                                "reason": "分享写代码与全栈开发经验"}])
    approved = await scout.judge(_FakeAI(), [("程序猿日记", "写代码的全栈开发者")])
    check("侦察兵确定性排除技术向内容源（不进白名单）", approved == [])
    scout_entries = scout._registry.get("entries", [])
    check("该源被记入注册表且 verdict=reject",
          any(e.get("name") == "程序猿日记" and e.get("verdict") == "reject" for e in scout_entries))
    Path(sdyn.name).unlink(missing_ok=True)

    print("\n=== 17. 公众号稳定化（WeWe-RSS 解析 + DDG 发现 + 侦察兵挖账号名）===")
    # 17.1 WeWe-RSS 的 all.rss 解析：保留公众号名 + 按白名单过滤
    wx_rss = """<rss version="2.0"><channel>
      <item><title>阿猫读书：普通人如何靠读书账号变现</title>
        <link>https://mp.weixin.qq.com/s/abc123</link>
        <author>阿猫读书</author><description>这是一篇关于读书变现的复盘</description></item>
      <item><title>findyi：副业月入过万的方法</title>
        <link>https://mp.weixin.qq.com/s/def456</link>
        <author>findyi</author><description>副业实操</description></item>
      <item><title>技术干货：用 Python 写爬虫</title>
        <link>https://mp.weixin.qq.com/s/zzz</link>
        <author>代码哥</author><description>写代码教程</description></item>
    </channel></rss>"""
    parsed = WeixinWhitelistSource._parse_rss(wx_rss, ["阿猫读书", "findyi"], 3)
    names = [it.source_name for it in parsed]
    check("WeWe-RSS 解析保留公众号名(阿猫读书)", any("阿猫读书" in n for n in names))
    check("WeWe-RSS 解析保留公众号名(findyi)", any("findyi" in n for n in names))
    check("WeWe-RSS 按白名单过滤掉非名单账号(代码哥)", not any("代码哥" in n for n in names))
    check("WeWe-RSS 解析条数=2（仅白名单）", len(parsed) == 2)

    # 17.2 DDG site:mp.weixin 结果解析（稳定发现公众号文章）
    ddg_html = ('<a class="result__a" href="/l/?uddg=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Faaa">'
                '一人公司赚钱复盘</a><a class="result__snippet">这是摘要</a>'
                '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fb">不相关</a>')
    ddg_items = WeixinSearchSource._parse_ddg_html(ddg_html, 5)
    check("DDG 解析只保留 mp.weixin 文章", len(ddg_items) == 1)
    check("DDG 解析解出真实链接", ddg_items[0].url == "https://mp.weixin.qq.com/s/aaa")
    check("DDG 解析标题正确", ddg_items[0].title == "一人公司赚钱复盘")

    # 17.3 侦察兵从微信文章挖账号名 + 从文章页提取昵称
    from sources.source_scout import SourceScout as _SS
    check("侦察兵从 ContentItem 取公众号名",
          _SS._account_from_item(ContentItem(title="x", url="u", source="weixin_whitelist",
                                             source_name="公众号·阿猫读书")) == "阿猫读书")
    check("侦察兵非微信项无账号名",
          _SS._account_from_item(ContentItem(title="x", url="u", source="rss",
                                             source_name="中文搜索")) == "")
    page_html = 'var nickname = "普通人小张"; <span class="profile_nickname">普通人小张</span>'
    check("侦察兵从文章页提取公众号名", _SS._extract_nickname(page_html) == "普通人小张")

    # 17.4 侦察兵 gather_candidates：从已采集内容挖账号名（稳定，不依赖搜索）
    # 用新临时文件，避免读到上方已写的注册表
    import tempfile as _tf
    s2 = _tf.NamedTemporaryFile(suffix=".json", delete=False); s2.close()
    scout2 = SourceScout(s2.name, {"min_score": 7})
    sample_items = [
        ContentItem(title="读书变现文", url="https://mp.weixin.qq.com/s/a1",
                    source="weixin_whitelist", source_name="公众号·阿猫读书", summary="读书赚钱"),
        ContentItem(title="副业文", url="https://mp.weixin.qq.com/s/a2",
                    source="weixin", source_name="公众号·findyi", summary="副业月入"),
        ContentItem(title="无关文", url="https://example.com/x",
                    source="rss", source_name="中文搜索"),
    ]
    cands = await scout2.gather_candidates(None, sample_items)  # client=None → DDG 路跳过
    cand_names = [a for a, _ in cands]
    check("侦察兵从采集内容挖到 阿猫读书", "阿猫读书" in cand_names)
    check("侦察兵从采集内容挖到 findyi", "findyi" in cand_names)
    check("侦察兵忽略非微信项", "中文搜索" not in cand_names)
    Path(s2.name).unlink(missing_ok=True)


    print("\n=== 18. 公众号目标号轮询（多引擎 + 作者昵称验证）===")
    from sources.weixin_targets import (
        WeixinTargetSource, extract_nickname, parse_baidu_results,
        parse_sogou_results, nickname_matches, load_targets,
    )
    # 18.1 纯函数：搜狗微信结果解析（提取 link?url= 跳转链接）
    sogou_html = (
        '<a href="https://weixin.sogou.com/link?url=AAA">读书变现文</a>'
        '<a href="https://weixin.sogou.com/link?url=BBB">别人号文</a>'
    )
    sres = parse_sogou_results(sogou_html, 5)
    check("搜狗解析提取 2 条跳转链接", len(sres) == 2)
    check("搜狗解析均为 weixin.sogou.com/link 链接",
          all("weixin.sogou.com/link?url=" in h for _, h in sres))

    # 18.2 纯函数：Baidu mu= 直链解析
    baidu_html = (
        'mu="https://mp.weixin.qq.com/s?__biz=1&amp;mid=2" '
        'mu="https://mp.weixin.qq.com/s?__biz=3" '
        'mu="https://other.com/x"'
    )
    bres = parse_baidu_results(baidu_html, 5)
    check("Baidu mu= 解析只留 mp.weixin 直链(2条)", len(bres) == 2)
    check("Baidu mu= 直链为真实文章 URL",
          all("mp.weixin.qq.com/s?" in h for _, h in bres))

    # 18.3 纯函数：作者昵称提取 + 归一化匹配（号名≠显示名，如临公子）
    check("提取作者昵称(临公子)", extract_nickname('var nickname = "临公子";') == "临公子")
    check("提取作者昵称(无→空串)", extract_nickname("<html>无昵称</html>") == "")
    check("归一化精确匹配(临公子)",
          nickname_matches("临公子", "临公子"))
    check("归一化容错子串(临公子的后花园→临公子)",
          nickname_matches("临公子", "临公子的后花园"))
    check("不匹配返回 False(代码哥)", not nickname_matches("代码哥", "阿猫读书"))

    # 18.4 load_targets：读列表 + 显示名映射
    targets = load_targets()
    tnames = [t["name"] for t in targets]
    check("目标列表含 阿猫读书", "阿猫读书" in tnames)
    lg = next(t for t in targets if t["name"] == "临公子的后花园")
    check("临公子的后花园 验证用显示名=临公子", lg["weixin_id"] == "临公子")

    # 18.5 不碰网络：FakeClient 验证「多引擎顺序 + 作者不匹配则过滤」
    class _FakeResp:
        def __init__(self, text="", url="", status=200):
            self.text = text; self._url = url; self.status_code = status
        @property
        def url(self): return self._url
    class _FakeClient:
        def __init__(self): self.calls = []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, timeout=15):
            self.calls.append(url)
            if "weixin.sogou.com/weixin" in url:          # 搜狗搜索页
                return _FakeResp(sogou_html, url)
            if "weixin.sogou.com/link?url=AAA" in url:    # 跟进→真实文章(目标号)
                return _FakeResp('var nickname = "阿猫读书";',
                                url="https://mp.weixin.qq.com/s/match")
            if "weixin.sogou.com/link?url=BBB" in url:    # 跟进→真实文章(别人号)
                return _FakeResp('var nickname = "别人号";',
                                url="https://mp.weixin.qq.com/s/wrong")
            if "baidu.com/s" in url:                      # 百度搜索页
                return _FakeResp(
                    'mu="https://mp.weixin.qq.com/s?__biz=match"', url)
            if "mp.weixin.qq.com/s/match" in url:
                return _FakeResp('var nickname = "阿猫读书";', url)
            if "mp.weixin.qq.com" in url:
                return _FakeResp('var nickname = "别人号";', url)
            return _FakeResp("", url, 404)
    src = WeixinTargetSource()
    items = await src._search_account(
        _FakeClient(), ["sogou", "baidu", "ddg"], "阿猫读书", "阿猫读书", 4, 15)
    check("FakeClient：命中目标号产出 1 条", len(items) == 1, f"got {len(items)}")
    check("FakeClient：产出为真实文章链接",
          len(items) == 1 and items[0].url == "https://mp.weixin.qq.com/s/match")
    check("FakeClient：作者不匹配被过滤(仅留1条)", len(items) == 1)

    # 18.6 引擎回退：sogou 0 候选时落到 baidu
    class _SogouEmpty:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, timeout=15):
            return _FakeResp('<div>no result</div>', url)
    class _BaiduOnly:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, timeout=15):
            if "baidu.com/s" in url:
                return _FakeResp(
                    'mu="https://mp.weixin.qq.com/s?__biz=match"', url)
            if "mp.weixin.qq.com/s?__biz=match" in url:
                return _FakeResp('var nickname = "阿猫读书";', url)
            return _FakeResp("", url, 404)
    items2 = await src._search_account(
        _BaiduOnly(), ["sogou", "baidu"], "阿猫读书", "阿猫读书", 4, 15)
    check("引擎回退：sogou空→baidu命中 1 条", len(items2) == 1, f"got {len(items2)}")

    print("\n=== 19. B站（哔哩哔哩）源 ===")
    from sources.bilibili import BilibiliSource, parse_bilibili_results
    sample = {
        "code": 0,
        "data": {"result": [
            {"result_type": "video", "data": [
                {"bvid": "BV1xx", "title": 'DIY做不下去了！<em class="keyword">副业</em>干点啥',
                 "author": "张三说钱", "arcurl": "http://www.bilibili.com/video/av1",
                 "description": "普通人副业实操", "tag": "副业,一人公司",
                 "pic": "https://i0.hdslb.com/x.jpg", "play": 4780,
                 "pubdate": 1700000000},
                {"bvid": "BV2yy", "title": "无关视频", "author": "李四",
                 "arcurl": "http://www.bilibili.com/video/av2", "description": "x",
                 "tag": "", "pic": "", "play": 1, "pubdate": 1700000100},
            ]},
            {"result_type": "user", "data": [{"uname": "某用户"}]},
        ]},
    }
    parsed = parse_bilibili_results(sample, 10)
    check("B站解析提取 2 支视频", len(parsed) == 2)
    check("B站解析剥离标题高亮标签", parsed[0]["title"] == "DIY做不下去了！副业干点啥")
    check("B站解析用 bvid 拼 URL", parsed[0]["url"] == "https://www.bilibili.com/video/BV1xx")
    check("B站解析含作者", parsed[0]["author"] == "张三说钱")

    # 19.2 不碰网络：FakeClient 验证 fetch 产出 ContentItem
    class _BiliResp:
        def __init__(self, data, status=200): self._data = data; self.status_code = status
        def json(self): return self._data
    class _BiliClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, timeout=20):
            return _BiliResp(sample)
    bsrc = BilibiliSource()
    bitems = await bsrc.fetch(
        {"enabled": True, "queries": ["副业"], "max_per_query": 5,
         "max_total": 20, "query_delay": 0}, {}, _BiliClient())
    check("B站 fetch 产出 2 条", len(bitems) == 2, f"got {len(bitems)}")
    check("B站 fetch source= bilibili", bitems[0].source == "bilibili")
    check("B站 fetch source_name 带『B站·』前缀",
          bitems[0].source_name.startswith("B站·"))
    check("B站 fetch summary 含标签", "一人公司" in bitems[0].summary)


def run():
    try:
        asyncio.run(main_tests())
        asyncio.run(scout_tests())
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
    print("🎉 全部通过，无崩溃路径。")


if __name__ == "__main__":
    run()
