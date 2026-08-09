#!/usr/bin/env python3
"""
客观打分 / 跨天机会库 / 反馈闭环 — 单元测试（不碰网络、不需要 AI key）

覆盖：
  1. scoring.py   打分公式的分布校准、硬性封顶、AI 漏给因子时的降级保护
  2. library.py   主题指纹相似度、跨天归并、同日去重、持久化、本周风向
  3. feedback.py  反馈链接生成、偏好调序
  4. push/feishu  演练模式绝不发网络请求

运行： cd daily-opportunity-bot && .venv/Scripts/python tests/test_scoring_library_feedback.py
"""
import asyncio
import json
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scoring
import library as lib_mod
from library import OpportunityLibrary
from feedback import build_feedback_urls, PreferenceProfile
from sources.base import ContentItem
from push.feishu import FeishuPusher

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _all(v):
    """所有 AI 打分因子统一给 v 分。"""
    return {k: v for k in scoring.AI_RATED_KEYS}


# ============================================================
# 1. 打分公式
# ============================================================

def scoring_tests():
    print("\n===== 1. 客观打分公式 =====")

    # --- 分布校准：分数要能拉开档次，中位数不能显得"还不错" ---
    best = scoring.compute(_all(5), code_dependency=1, authenticity=5)
    good = scoring.compute(_all(4), code_dependency=1, authenticity=4)
    mid = scoring.compute(_all(3), code_dependency=3, authenticity=3)
    weak = scoring.compute(_all(2), code_dependency=3, authenticity=3)
    worst = scoring.compute(_all(1), code_dependency=5, authenticity=1)

    check("满分机会 = 10 分", best["startup_index"] == 10, str(best["startup_index"]))
    check("各项 4 分 = 8 分", good["startup_index"] == 8, str(good["startup_index"]))
    check("全中位数只给 5 分（平庸就该看起来平庸）",
          mid["startup_index"] == 5, str(mid["startup_index"]))
    check("各项 2 分 ≤ 3 分", weak["startup_index"] <= 3, str(weak["startup_index"]))
    check("全 1 分 = 1 分", worst["startup_index"] == 1, str(worst["startup_index"]))
    check("分数单调不降",
          best["startup_index"] > good["startup_index"] > mid["startup_index"]
          > weak["startup_index"] >= worst["startup_index"])

    # --- 硬性封顶：致命短板必须一票压制，不能被高总分掩盖 ---
    code_heavy = scoring.compute(_all(5), code_dependency=5, authenticity=5)
    check("要写代码 → 封顶 3 分（用户红线）", code_heavy["startup_index"] <= 3,
          str(code_heavy["startup_index"]))
    check("要写代码时给出封顶原因", any("代码" in c for c in code_heavy["caps"]))

    shovel = scoring.compute(_all(5), code_dependency=1, authenticity=2)
    check("疑似卖铲子 → 封顶 3 分", shovel["startup_index"] <= 3, str(shovel["startup_index"]))

    no_channel = scoring.compute(dict(_all(5), channel=1), code_dependency=1, authenticity=5)
    check("没写清获客路径 → 封顶 6 分", no_channel["startup_index"] <= 6,
          str(no_channel["startup_index"]))

    cheap = scoring.compute(dict(_all(4), urgency=2, pricing=2), code_dependency=1, authenticity=4)
    check("需求不痛且卖不上价 → 封顶 5 分", cheap["startup_index"] <= 5,
          str(cheap["startup_index"]))

    # --- 降级保护：AI 没按格式给因子时，不能靠"默认中位数"混过门槛 ---
    empty = scoring.compute({}, code_dependency=2, authenticity=4)
    check("AI 完全没给 factors → 标记降级", empty["degraded"] is True)
    check("AI 完全没给 factors → 封顶 4 分", empty["startup_index"] <= 4,
          str(empty["startup_index"]))
    partial = scoring.compute({"urgency": 5, "pricing": 5, "channel": 5},
                              code_dependency=1, authenticity=5)
    check("AI 只给 3/11 项 → 仍判降级", partial["degraded"] is True)
    check("AI 只给 3/11 项 → 封顶 4 分", partial["startup_index"] <= 4,
          str(partial["startup_index"]))
    full = scoring.compute(_all(4), code_dependency=1, authenticity=4)
    check("因子给全 → 不降级", full["degraded"] is False)

    # --- 脏数据不能让公式崩 ---
    dirty = scoring.compute(
        {"urgency": "5", "market_size": None, "pricing": 99, "repeat": -3,
         "moat": "很高", "margin": 3.7, "evergreen": True,
         "channel": 4, "capital": 4, "speed": 4, "skill": 4},
        code_dependency="2", authenticity=None,
    )
    check("脏数据不抛异常且落在 1-10", 1 <= dirty["startup_index"] <= 10,
          str(dirty["startup_index"]))
    check("越界值被收敛到 1-5",
          all(1 <= v <= 5 for v in dirty["factors"].values()), str(dirty["factors"]))

    # --- 三个对外数字的范围 ---
    for name, r in (("满分", best), ("中位", mid), ("脏数据", dirty)):
        check(f"{name}：商业化 0-100", 0 <= r["commercial"] <= 100)
        check(f"{name}：可行性 0-100", 0 <= r["feasibility"] <= 100)

    # --- no_code 由 code_dependency 反算，不让 AI 重复判断 ---
    check("code_dependency=1 → no_code=5",
          scoring.code_dependency_to_no_code(1) == 5)
    check("code_dependency=5 → no_code=1",
          scoring.code_dependency_to_no_code(5) == 1)

    # --- 权重必须归一，否则分数没有可比性 ---
    cw = sum(w for _, _, w in scoring.COMMERCIAL_FACTORS)
    fw = sum(w for _, _, w in scoring.FEASIBILITY_FACTORS)
    check("商业化权重和为 1.0", abs(cw - 1.0) < 1e-9, f"{cw}")
    check("可行性权重和为 1.0", abs(fw - 1.0) < 1e-9, f"{fw}")

    # --- 提示词与公式同源：每个 AI 因子都要在评分标准里有定义 ---
    missing = [k for k in scoring.AI_RATED_KEYS if k not in scoring.FACTOR_RUBRIC]
    check("每个 AI 因子都在 FACTOR_RUBRIC 里有锚点定义", not missing, str(missing))

    # --- apply_to_item 正确写回 ---
    it = ContentItem(title="t", url="u", source_name="s")
    it.code_dependency, it.authenticity = 1, 4
    scoring.apply_to_item(it, _all(4))
    check("apply_to_item 写回 startup_index", it.startup_index > 0)
    check("apply_to_item 写回商业化/可行性",
          it.commercial_score > 0 and it.feasibility_score > 0)
    check("apply_to_item 覆盖 relevance_score 为 0-1",
          0 <= it.relevance_score <= 1, str(it.relevance_score))
    check("apply_to_item 生成中文理由", isinstance(it.score_reason, str))


# ============================================================
# 2. 跨天机会库
# ============================================================

def _mk(who, what, src="源A", idx=7):
    it = ContentItem(title=what, url=f"http://x/{who}{what}", source_name=src)
    it.copy_template = {"who": who, "what": what}
    it.startup_index = idx
    return it


def library_tests():
    print("\n===== 2. 跨天机会库 =====")

    # --- 相似度：同义改写要认出来，不同生意不能误并 ---
    same = [
        ("帮本地餐饮店做小红书代运营", "给本地餐厅代运营小红书账号"),
        ("用ChatGPT帮人写求职简历", "ChatGPT代写简历，一份收200"),
        ("给中小企业做AI客服话术培训", "面向中小企业的AI客服话术培训课"),
    ]
    diff = [
        ("帮本地餐饮店做小红书代运营", "用AI做简历优化服务收费199"),
        ("做付费社群卖育儿知识", "做付费社群卖考研资料"),
        ("帮健身教练做私域社群运营", "给瑜伽老师做小红书涨粉"),
        ("卖Notion模板给自由职业者", "做剪辑外包接单"),
    ]
    for a, b in same:
        s = lib_mod._sim(lib_mod._norm(a), lib_mod._norm(b))
        check(f"同义改写判为同一主题：{a[:12]}…", s >= lib_mod.SIM_THRESHOLD, f"sim={s:.3f}")
    for a, b in diff:
        s = lib_mod._sim(lib_mod._norm(a), lib_mod._norm(b))
        check(f"不同生意不误并：{a[:12]}…", s < lib_mod.SIM_THRESHOLD, f"sim={s:.3f}")

    # --- 金额是噪声，不该影响主题判定 ---
    check("价格数字被归一化掉",
          lib_mod._norm("代写简历199元") == lib_mod._norm("代写简历"),
          f'{lib_mod._norm("代写简历199元")} vs {lib_mod._norm("代写简历")}')

    # --- 跨天累积 ---
    tmp = Path(tempfile.mkdtemp()) / "lib.json"

    d1 = OpportunityLibrary(tmp); d1.load()
    day1 = [_mk("本地餐饮店", "小红书代运营", "公众号-甲"),
            _mk("求职者", "ChatGPT代写简历", "推特-乙")]
    d1.annotate(day1, "2026-08-06")
    check("首次出现记为第 1 次", day1[0].repeat_count == 1, str(day1[0].repeat_count))
    check("首次出现写入 first_seen", day1[0].first_seen == "2026-08-06")
    check("annotate 写入 topic_key", bool(day1[0].topic_key))
    d1.save()

    d2 = OpportunityLibrary(tmp); d2.load()
    day2 = [_mk("餐厅", "代运营小红书账号", "少数派"),
            _mk("宝妈", "付费社群卖育儿知识", "公众号-丙")]
    d2.annotate(day2, "2026-08-07")
    check("换个说法讲同一件事 → 累计到第 2 次",
          day2[0].repeat_count == 2, str(day2[0].repeat_count))
    check("不同来源讲同一件事 → 印证数 2",
          day2[0].corroborations == 2, str(day2[0].corroborations))
    check("first_seen 保持首日不变", day2[0].first_seen == "2026-08-06", day2[0].first_seen)
    check("新主题独立计数", day2[1].repeat_count == 1)
    d2.save()

    d3 = OpportunityLibrary(tmp); d3.load()
    day3 = [_mk("本地餐厅", "小红书账号代运营", "推特-丁", idx=8)]
    d3.annotate(day3, "2026-08-08")
    check("第三天累计到第 3 次", day3[0].repeat_count == 3, str(day3[0].repeat_count))

    dup = [_mk("餐饮店", "小红书代运营", "公众号-戊")]
    d3.annotate(dup, "2026-08-08")
    check("同一天再出现不重复计次",
          dup[0].repeat_count == 3, str(dup[0].repeat_count))
    check("同一天不同来源仍计入印证",
          dup[0].corroborations == 4, str(dup[0].corroborations))

    rec = d3.top_recurring(days=7, limit=3, min_times=2)
    check("本周风向取到反复出现的主题", len(rec) == 1, str(rec))
    check("风向条目含出现次数", rec and rec[0]["times"] == 3, str(rec))
    check("风向条目含最高启动指数", rec and rec[0]["best_startup_index"] == 8, str(rec))

    d3.save()
    raw = json.loads(tmp.read_text(encoding="utf-8"))
    check("落盘为合法 JSON 且有 entries", "entries" in raw)
    check("落盘保留 3 个主题", len(raw["entries"]) == 3, str(len(raw["entries"])))

    # --- 损坏的库文件不能让整个流程崩掉 ---
    bad = Path(tempfile.mkdtemp()) / "bad.json"
    bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
    broken = OpportunityLibrary(bad)
    try:
        broken.load()
        items = [_mk("A", "B")]
        broken.annotate(items, "2026-08-08")
        check("库文件损坏时自动重建、不抛异常", items[0].repeat_count == 1)
    except Exception as e:
        check("库文件损坏时自动重建、不抛异常", False, repr(e))

    # --- 缺 copy_template 时能退化到机会提示 ---
    bare = ContentItem(title="某人靠帮人整理衣柜月入八千", url="http://x/bare", source_name="源")
    bare.opportunity_hint = "上门衣柜整理服务"
    lib4 = OpportunityLibrary(Path(tempfile.mkdtemp()) / "l.json"); lib4.load()
    lib4.annotate([bare], "2026-08-08")
    check("没有可抄模板时退化到机会提示", bool(bare.topic_key), bare.topic_key)


# ============================================================
# 3. 反馈闭环
# ============================================================

def feedback_tests():
    print("\n===== 3. 反馈闭环 =====")

    repo = "wsjxphz-png/opc-digdaily"
    up, down = build_feedback_urls(repo, "t0001_abc", "小红书代运营 → 卖给本地餐厅")

    check("点赞链接指向本仓库 Issue", up.startswith(f"https://github.com/{repo}/issues/new"))
    q_up = urllib.parse.parse_qs(urllib.parse.urlparse(up).query)
    q_dn = urllib.parse.parse_qs(urllib.parse.urlparse(down).query)
    check("点赞标题预填 [想做]", q_up["title"][0].startswith("[想做]"), q_up["title"][0])
    check("点踩标题预填 [没兴趣]", q_dn["title"][0].startswith("[没兴趣]"), q_dn["title"][0])
    check("点赞带标签", "想做" in q_up["labels"][0], q_up["labels"][0])
    check("点踩带标签", "没兴趣" in q_dn["labels"][0], q_dn["labels"][0])
    check("正文预填机会编号（回读时靠它对上号）",
          "t0001_abc" in q_up["body"][0], q_up["body"][0][:80])

    # --- 偏好调序：点过想做的上浮，且幅度克制 ---
    tmp = Path(tempfile.mkdtemp()) / "lib.json"
    lib = OpportunityLibrary(tmp); lib.load()
    seed = [_mk("本地餐厅", "小红书代运营", idx=6),
            _mk("求职者", "ChatGPT代写简历", idx=6)]
    lib.annotate(seed, "2026-08-08")
    keys = {i.copy_template["what"]: i.topic_key for i in seed}
    n = lib.apply_feedback({keys["小红书代运营"]: {"up": 1, "down": 0}})
    check("反馈能并入机会库", n == 1, str(n))

    prof = PreferenceProfile.from_library(lib, 1, 2)
    check("有反馈时偏好档案生效", prof.active)
    check("识别出 1 类喜欢的方向", len(prof.liked) == 1, str(len(prof.liked)))

    new = [_mk("餐厅", "代运营小红书账号", idx=6),
           _mk("自由职业者", "卖模板", idx=6)]
    before = new[0].startup_index
    prof.adjust(new)
    liked = [i for i in new if "小红书" in i.copy_template["what"]][0]
    other = [i for i in new if "模板" in i.copy_template["what"]][0]
    check("点过想做的方向分数上浮", liked.startup_index > before,
          f"{before} → {liked.startup_index}")
    check("上浮幅度克制（≤2 分）", liked.startup_index - before <= 2,
          f"+{liked.startup_index - before}")
    check("无关方向不受影响", other.startup_index == 6, str(other.startup_index))
    check("调整后按分数重排", new[0].startup_index >= new[-1].startup_index)

    # --- 没有任何反馈时不应改动排序 ---
    empty_lib = OpportunityLibrary(Path(tempfile.mkdtemp()) / "e.json"); empty_lib.load()
    prof0 = PreferenceProfile.from_library(empty_lib, 1, 2)
    check("没有反馈时偏好档案不生效", not prof0.active)


# ============================================================
# 4. 演练模式
# ============================================================

async def dryrun_tests():
    print("\n===== 4. 演练模式 =====")

    sent = {"n": 0}

    class Boom:
        """演练模式下一旦真的发网络请求，就让测试失败。"""
        def __init__(self, *a, **k): sent["n"] += 1
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise AssertionError("演练模式竟然发出了请求")

    import push.feishu as fs
    real_client = fs.httpx.AsyncClient
    fs.httpx.AsyncClient = Boom
    try:
        it = _mk("本地餐厅", "小红书代运营", idx=8)
        it.ai_summary = "摘要"
        it.code_dependency, it.authenticity = 1, 4
        it.copy_template.update({"first_step": "列20家店", "first_prompt": "写5条标题", "cost": "0元"})
        scoring.apply_to_item(it, _all(4))

        p = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/FAKE",
                         feedback_repo="wsjxphz-png/opc-digdaily",
                         feedback_enabled=True, dry_run=True)
        check("演练模式即使配了 webhook 也算启用（好渲染卡片）", p.enabled)
        ok = await p.push_opportunities([it], [], "2026-08-09", None)
        check("演练推送返回成功", ok)
        check("演练模式一个网络请求都没发", sent["n"] == 0, f"发了{sent['n']}次")
    finally:
        fs.httpx.AsyncClient = real_client

    # --- 卡片内容完整性 ---
    p2 = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/FAKE",
                      feedback_repo="wsjxphz-png/opc-digdaily", feedback_enabled=True)
    it = _mk("本地餐厅", "小红书代运营", idx=8)
    it.ai_summary = "一位宝妈帮6家餐厅代运营小红书"
    it.code_dependency, it.authenticity = 1, 4
    it.copy_template.update({"first_step": "列20家店", "first_prompt": "写5条标题", "cost": "0元"})
    scoring.apply_to_item(it, _all(4))
    it.repeat_count, it.corroborations, it.first_seen = 3, 2, "2026-08-06"
    recurring = [{"topic": "小红书代运营 → 卖给本地餐厅", "times": 3, "sources": 2,
                  "first_seen": "2026-08-06", "best_startup_index": 8}]
    card = p2._build_dual_card([it], [], "2026-08-09", 1, recurring=recurring)
    s = json.dumps(card, ensure_ascii=False)
    for kw, label in [("适合你启动", "启动指数"), ("商业化潜力", "商业化分"),
                      ("可行性", "可行性分"), ("可抄模板", "可抄模板"),
                      ("第 3 次出现", "重复出现标注"), ("想做", "反馈按钮"),
                      ("本周反复出现", "本周风向")]:
        check(f"卡片包含{label}", kw in s)
    check("卡片未渲染出「未知」占位", "· 未知" not in s)
    size = len(s.encode("utf-8"))
    check("卡片体积在飞书 30KB 限制内", size < 30000, f"{size} 字节")


# ============================================================
# 5. 大批量自动分块（process 不再因截断丢整批）
# ============================================================

def _mk_full(who, what, src="源A", idx=7, full=""):
    it = _mk(who, what, src, idx)
    it.full_text = full
    return it


async def chunking_tests():
    print("\n===== 5. 大批量自动分块 =====")
    from ai import AIProcessor

    def make_items(n):
        items = []
        for i in range(n):
            it = ContentItem(title=f"机会{i}", url=f"http://x/{i}", source_name="S")
            it.full_text = "x" * 1300  # 长全文 → 触发 token 预算切分
            items.append(it)
        return items

    proc = AIProcessor("https://api", "sk-" + "x" * 30, "m", max_tokens=8000)

    # --- _chunk_items 边界 ---
    items = make_items(172)
    chunks = proc._chunk_items(items)
    check("172 条被拆成多块（>1）", len(chunks) > 1, f"{len(chunks)} 块")
    flat = [it for c in chunks for it in c]
    check("分块不丢条目、不重复",
          len(flat) == 172 and {id(x) for x in flat} == {id(x) for x in items})
    check("单块不超过硬上限 18", all(len(c) <= 18 for c in chunks))

    # 长全文应比短全文切得更碎（短全文被输出预算限到 ~10/块，长全文被输入预算提前切开）
    short = [ContentItem(title=f"机会{i}", url=f"http://x/{i}", source_name="S")
             for i in range(60)]  # full_text 为空
    long = make_items(60)         # full_text 长
    chunks_short = proc._chunk_items(short)
    chunks_long = proc._chunk_items(long)
    check("短/长全文都会被拆成多块", len(chunks_short) > 1 and len(chunks_long) > 1)

    # 输入 token 预算确实生效：把 max_input_tokens 调小，长全文每块累计输入应被压在预算内
    proc_tight = AIProcessor("https://api", "sk-" + "x" * 30, "m", max_tokens=8000)
    chunks_tight = proc_tight._chunk_items(long, max_input_tokens=2000)
    max_in = max(
        (sum(proc_tight._est_input_tokens(it) for it in c) for c in chunks_tight),
        default=0,
    )
    check("输入 token 预算约束生效（每块累计 ≤ 预算+单条上限）",
          max_in <= 2000 + 632, f"最大块 {max_in} tokens")

    # --- mock _chat：按块内 index 返回合法 JSON，验证多块聚合 + 顺序 ---
    async def fake_chat(messages, max_tokens, temperature):
        import re as _re
        user = messages[1]["content"]
        idxs = [int(m) for m in _re.findall(r"\[(\d+)\]", user)]
        arr = []
        for i in idxs:
            arr.append({
                "index": i, "relevant": True,
                "translation": "", "summary": f"摘要{i}",
                "opportunity_hint": "卖X去Y找Z",
                "code_dependency": 1, "authenticity": 4,
                "practical_steps": "1.交付 2.客户 3.工具",
                "verdict": "可复刻的真机会", "difficulty": "零门槛",
                "quality_flag": "",
                "factors": {"urgency": 4, "market_size": 3, "pricing": 4,
                            "repeat": 3, "moat": 2, "margin": 4, "evergreen": 4,
                            "channel": 4, "capital": 5, "speed": 4, "skill": 5},
                "copy_template": {"who": "A", "what": "B",
                                  "first_step": "C", "first_prompt": "D", "cost": "0元"},
            })
        return json.dumps(arr, ensure_ascii=False)

    proc._chat = fake_chat
    out = await proc.process(make_items(172))
    n_ok = sum(1 for it in out if getattr(it, "ai_processed", False))
    check("分块后全部 172 条都被 AI 处理", n_ok == 172, f"{n_ok}/172")
    check("聚合结果顺序与输入一致",
          [it.url for it in out] == [f"http://x/{i}" for i in range(172)])

    # --- 降级：某块持续失败，只丢那一块，其余照常 ---
    #   每块内容是局部从 0 重新编号的，用全局唯一标题「机会0」只让第 0 块失败
    async def flaky_chat(messages, max_tokens, temperature):
        user = messages[1]["content"]
        if "机会0" in user:  # 仅含全局 index 0 的那块 → 故意失败
            return None
        return await fake_chat(messages, max_tokens, temperature)

    proc2 = AIProcessor("https://api", "sk-" + "x" * 30, "m", max_tokens=8000)
    proc2._chat = flaky_chat
    out2 = await proc2.process(make_items(172))
    processed = sum(1 for it in out2 if getattr(it, "ai_processed", False))
    check("单块持续失败只降级该块（其余仍被处理）",
          150 < processed < 172, f"成功 {processed}/172")
    check("失败块的条目 ai_processed=False",
          any(not getattr(it, "ai_processed", False) for it in out2))

    # --- 自适应减半重试：被截断的块应被拆小后恢复，不丢数据 ---
    async def recover_chat(messages, max_tokens, temperature):
        user = messages[1]["content"]
        # 含「机会0」且块还很大（>1 条）时假装被截断（返回空）→ 触发减半重试
        if "机会0" in user and user.count("[") > 1:
            return None
        return await fake_chat(messages, max_tokens, temperature)

    proc3 = AIProcessor("https://api", "sk-" + "x" * 30, "m", max_tokens=8000)
    proc3._chat = recover_chat
    out3 = await proc3.process(make_items(172))
    processed3 = sum(1 for it in out3 if getattr(it, "ai_processed", False))
    check("被截断的块经减半重试后全部恢复（0 丢失）",
          processed3 == 172, f"成功 {processed3}/172")
    check("减半重试后所有条目都被处理",
          all(getattr(it, "ai_processed", False) for it in out3))


def run():
    try:
        scoring_tests()
        library_tests()
        feedback_tests()
        asyncio.run(dryrun_tests())
        asyncio.run(chunking_tests())
    except Exception:
        import traceback
        traceback.print_exc()
        RESULTS.append(("未捕获异常", False, ""))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"结果: {passed}/{total} 通过")
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    if failed:
        print("失败项:")
        for n, d in failed:
            print(f"  · {n}  {d}")
        sys.exit(1)
    print("🎉 全部通过。")


if __name__ == "__main__":
    run()
