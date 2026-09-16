#!/usr/bin/env python3
"""
模块2 卡片超限二分重发 — 回归测试（不碰网络）

背景：实测每条机会卡约 4.4KB（其中两个反馈按钮的 URL 占 2482 字节），
10 条即 44KB，远超飞书 25KB 卡点。旧逻辑超限时只降一层（按板块拆 国内/国际），
板块卡自己仍超限就整块丢弃——9/11、9/15、9/16 连续三天漏送国内机会即因此
（9/16 实测：卡 44480B → 拆出国内卡 31331B 仍超限 → 国内 7 条全丢、国际 3 条送达）。

本测试锁死四条不变量：
  1. 发出去的每张卡都必须 ≤ 25KB（否则真实环境会被 _send_card 拒发）
  2. 所有条目必须全部送达（不丢内容）
  3. 没有条目被重复发出（不重发）
  4. 只有「超限」才触发拆分；网络类失败不拆（否则请求数放大到 2n-1）

运行： cd opc-digdaily && python tests/test_card_split.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# 本机 Windows 控制台默认 GBK，编不出 ✅ 会抛 UnicodeEncodeError 让整轮假红
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not os.environ.get("OPC_STORAGE_DIR"):
    os.environ["OPC_STORAGE_DIR"] = str(Path(tempfile.mkdtemp(prefix="opc_test_storage_")))

from sources.base import ContentItem
from push import FeishuPusher

RESULTS = []
LIMIT = 25 * 1024
URL_RE = re.compile(r"http://example\.com/opp/\d+")
REC_MARK = "本周反复出现的方向"  # _build_recurring_block 渲染出的实际文案


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


def mk_opp(i: int, region: str = "国内") -> ContentItem:
    """构造一条贴近生产体量的机会条目（实测约 4.4KB/条，含反馈按钮 URL）。"""
    it = ContentItem(
        title=f"{region}第{i}条：某人为本地小商家做月度素材包并订阅收费的完整复盘",
        url=f"http://example.com/opp/{i}",
        summary="x" * 200,
        source_name="r/opps",
        source="rss",
    )
    it.translation = "y" * 280
    it.ai_summary = "z" * 420
    it.opportunity_hint = "w" * 40
    it.difficulty = "零门槛"
    it.practical_steps = (
        "1. 交付物：" + "a" * 100 + "\n"
        "2. 前5个客户：" + "b" * 100 + "\n"
        "3. 工具链：" + "c" * 100
    )
    it.copy_template = {
        "who": "d" * 50,
        "what": "e" * 50,
        "first_step": "f" * 80,
        "first_prompt": "g" * 220,
        "cost": "h" * 40,
    }
    it.startup_index = 8
    it.relevance_score = 0.9
    it.ai_processed = True
    it.topic_key = f"topic{i}"
    return it


def make_pusher():
    """返回 (pusher, accepted, rejected)。

    accepted: [(字节数, {条目url}), ...] —— 真正发出去的卡
    rejected: [(字节数, {条目url}), ...] —— 被大小闸门拒掉的卡（没发网络请求）
    """
    # 必须开反馈按钮：真实卡片里两个按钮的 URL 占 2482 字节（约总量的 57%），
    # 关掉就模拟不出真实体量，测试会假绿。
    p = FeishuPusher(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test", "blue",
        feedback_repo="wsjxphz-png/opc-digdaily", feedback_enabled=True,
    )
    accepted, rejected = [], []

    async def fake_send(card):
        """复刻真实 _send_card 的大小闸门（超 25KB 直接拒发，不发网络请求）。"""
        raw = json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False)
        size = len(raw.encode("utf-8"))
        entry = (size, set(URL_RE.findall(raw)), raw)
        ok = size <= LIMIT
        # 必须复刻真实 _send_card 的契约：失败时写明原因，调用方据此决定要不要二分
        p._last_reject = None if ok else "too_large"
        (accepted if ok else rejected).append(entry)
        return ok

    p._send_card = fake_send
    return p, accepted, rejected


def urls_of(cards):
    out = []
    for _, urls, _raw in cards:
        out.extend(sorted(urls))
    return out


def texts_of(cards):
    return "\n".join(raw for _, _, raw in cards)


async def main_tests():
    print("\n=== 1. 单卡装得下时不拆卡（保持「一屏看完」） ===")
    p, acc, rej = make_pusher()
    dom, intl = [mk_opp(i) for i in range(2)], [mk_opp(100 + i) for i in range(1)]
    ok = await p.push_opportunities(dom, intl, "2026年09月17日")
    check("3 条 → 返回 True", ok)
    check("3 条 → 只发 1 张卡", len(acc) == 1, f"发了 {len(acc)} 张")
    check("3 条 → 无卡被拒", not rej, f"被拒 {len(rej)} 张")

    print("\n=== 2. 单卡超限 → 二分拆到全部装下（本 bug 的核心回归） ===")
    p, acc, rej = make_pusher()
    dom = [mk_opp(i) for i in range(7)]      # 9/16 的真实分布：国内 7 + 国际 3
    intl = [mk_opp(100 + i) for i in range(3)]
    ok = await p.push_opportunities(dom, intl, "2026年09月17日")
    check("10 条 → 返回 True（全部送达）", ok)
    check("每张发出的卡都 ≤ 25KB", all(s <= LIMIT for s, _, _ in acc),
          f"最大 {max((s for s, _, _ in acc), default=0)}B")
    check("拆成了多张卡", len(acc) > 1, f"{len(acc)} 张")
    check("10 条全部标记送达", p._delivered_urls == {it.url for it in dom + intl},
          f"送达 {len(p._delivered_urls)}/10")

    print("\n=== 3. 大板块（旧逻辑必然整块丢的场景） ===")
    p, acc, rej = make_pusher()
    dom = [mk_opp(i) for i in range(20)]     # 9/14 的真实分布：国内 20 条
    ok = await p.push_opportunities(dom, [], "2026年09月17日")
    check("20 条 → 返回 True", ok)
    check("每张发出的卡都 ≤ 25KB", all(s <= LIMIT for s, _, _ in acc))
    check("20 条全部送达", p._delivered_urls == {it.url for it in dom},
          f"送达 {len(p._delivered_urls)}/20")
    print(f"     （二分中被拒 {len(rej)} 张，最终发出 {len(acc)} 张）")

    print("\n=== 4. 不重复发送、不遗漏（逐卡核对条目，不是只数个数） ===")
    p, acc, rej = make_pusher()
    dom = [mk_opp(i) for i in range(9)]
    intl = [mk_opp(100 + i) for i in range(3)]
    await p.push_opportunities(dom, intl, "2026年09月17日")
    sent_urls = urls_of(acc)
    dups = {u for u in sent_urls if sent_urls.count(u) > 1}
    check("没有条目出现在两张卡里", not dups, f"重复 {sorted(dups)}")
    check("卡片覆盖的条目 = 全部 12 条",
          set(sent_urls) == {it.url for it in dom + intl},
          f"覆盖 {len(set(sent_urls))}/12")

    print("\n=== 5. 超批路径（batch_size 很小时） ===")
    p, acc, rej = make_pusher()
    p._batch_size = 3
    dom = [mk_opp(i) for i in range(8)]
    intl = [mk_opp(100 + i) for i in range(4)]
    ok = await p.push_opportunities(dom, intl, "2026年09月17日")
    check("超批 12 条 → 返回 True", ok)
    check("超批 → 每张卡 ≤ 25KB", all(s <= LIMIT for s, _, _ in acc))
    check("超批 → 12 条全部送达", len(p._delivered_urls) == 12,
          f"送达 {len(p._delivered_urls)}/12")

    print("\n=== 6. 只有国际、国内为空：本周风向块不能丢 ===")
    rec = [{"topic": "给本地商家做素材订阅", "times": 3, "sources": 2,
            "first_seen": "2026-09-10"}]

    # 基线：有国内时风向块出现在第一张卡里
    p_dom, acc_dom, _ = make_pusher()
    await p_dom.push_opportunities([mk_opp(0)], [mk_opp(100)], "2026年09月17日", recurring=rec)
    check("对照：有国内时风向块出现", REC_MARK in texts_of(acc_dom),
          f"出现 {texts_of(acc_dom).count(REC_MARK)} 次")

    # 回归：国内为空时旧代码会把风向块静默丢掉（且推送仍算成功）
    p_intl, acc_intl, _ = make_pusher()
    intl = [mk_opp(100 + i) for i in range(8)]
    await p_intl.push_opportunities([], intl, "2026年09月17日", recurring=rec)
    check("国际-only 时 8 条全部送达", len(p_intl._delivered_urls) == 8,
          f"送达 {len(p_intl._delivered_urls)}/8")
    check("国际-only 时风向块仍在", REC_MARK in texts_of(acc_intl),
          f"出现 {texts_of(acc_intl).count(REC_MARK)} 次")
    check("国际-only 时风向块不重复",
          texts_of(acc_intl).count(REC_MARK) == 1,
          f"出现 {texts_of(acc_intl).count(REC_MARK)} 次")

    print("\n=== 7. 网络类失败不触发二分（请求数不放大） ===")
    p, acc, rej = make_pusher()
    calls = {"n": 0}

    async def failing_send(card):
        calls["n"] += 1
        p._last_reject = "send_failed"   # 模拟飞书限流/网络错误
        return False

    p._send_card = failing_send
    dom = [mk_opp(i) for i in range(20)]
    ok = await p.push_opportunities(dom, [], "2026年09月17日")
    check("网络失败 → 返回 False（如实上报）", not ok)
    check("网络失败 → 不二分放大请求", calls["n"] <= 2,
          f"打了 {calls['n']} 次（无此护栏时二分会让 20 条打到约 39 次）")


def run():
    try:
        asyncio.run(main_tests())
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
