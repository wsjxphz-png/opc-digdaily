#!/usr/bin/env python3
"""生成模块2「赚钱机会挖掘」示例卡片（飞书 JSON + Markdown 预览），不触发真实推送。

演示新规则：
- 国际内容：中文翻译做主标题，英文原标题降级为脚注（能翻译就翻译，少英文）
- 获客路径是每张卡最该讲透的部分
"""
import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(ROOT))

from sources.base import ContentItem
from push import FeishuPusher


def mk(title, summary, hint, steps, verdict, code, auth, score, url, src, translation=""):
    it = ContentItem(title=title, url=url, summary=summary, source_name=src)
    it.ai_summary = summary
    it.opportunity_hint = hint
    it.practical_steps = steps
    it.verdict = verdict
    it.code_dependency = code
    it.authenticity = auth
    it.relevance_score = score
    it.translation = translation  # 英文标题的中文翻译（国内源留空）
    it.ai_processed = True
    return it


async def main():
    domestic = [
        mk(
            "有人靠整理全国宠物友好餐厅清单，在小程序卖年度会员",
            "她用半年人工搜集 2000 家宠物友好餐厅，做成可筛选小程序，年费 99 元，靠小红书引流，已 3000 付费会员。完整讲了怎么找第一批餐厅、怎么谈合作。",
            "卖「整理好的信息差」× 去小红书/社群找养宠人群 × 收 99 元/年。",
            "1.交付物：可筛选的宠物友好餐厅清单(小程序)\n2.前5个客户：小红书发笔记+评论区引流，第 3 篇爆款带来前 200 个会员\n3.工具链：表单/在线表格+小程序模板(无需写代码)",
            "可复刻的真机会", 2, 4, 0.85,
            "https://example.com/d1", "r/sidehustle-cn",
        ),
        mk(
            "退休阿姨靠代写回忆录，按小时收费做成产品化服务",
            "她把「帮老人整理口述史」包成固定 1999 元套餐，含 3 次访谈+排版成书，靠养老院和社区推荐获客，月接 8 单。",
            "卖「代写/整理服务」× 去社区/养老院谈合作 × 收 1999 元/单。",
            "1.交付物：一本排版好的回忆录(电子+打印)\n2.前5个客户：社区活动中心地推+老客户转介，前 2 单来自居委会推荐\n3.工具链：录音转文字+排版工具(现成)",
            "可复刻的真机会", 1, 4, 0.8,
            "https://example.com/d2", "r/service-cn",
        ),
    ]
    international = [
        mk(
            "I built a directory of remote-friendly companies and sell subscriptions",
            "他半年人工搜集了 3000 家允许远程办公的公司，做成可搜索网站，年费 49 美元。前 50 个客户来自在 Reddit 远程办公板块发的一篇冷启动帖、加上一个小众邮件通讯转载推荐，完整公开了成本和获客路径。",
            "卖「整理好的信息差清单」× 去 Reddit/邮件通讯找远程办公人群 × 收 49 美元/年。",
            "1.交付物：可搜索的远程公司清单(网站)\n2.前5个客户：在 Reddit 远程板块发冷启动帖+被一个小众邮件通讯转载，带来前 50 个付费用户\n3.工具链：无代码建站工具+付款链接(无需写代码)",
            "可复刻的真机会", 2, 5, 0.9,
            "https://example.com/i1", "r/indie",
            translation="有人整理了「支持远程办公的公司清单」，靠卖年度订阅赚钱",
        ),
        mk(
            "A solo consultant helps local clinics automate appointment reminders, $800/month retainer",
            "她用无代码工具为 12 家诊所搭了自动预约提醒系统，每家每月收 800 美元。前 3 个客户靠给本地牙医诊所发冷邮件（发了 20 封拿下 3 个），完整公开了搭建流程。",
            "卖「代搭自动化服务」× 给本地诊所/商家发冷邮件 × 收 800 美元/月。",
            "1.交付物：一套自动预约提醒系统(帮客户搭好)\n2.前3个客户：在地图搜本地牙医，冷邮件发 20 封拿下 3 家\n3.工具链：无代码自动化工具+表单(无需写代码)",
            "可复刻的真机会", 2, 4, 0.82,
            "https://example.com/i2", "r/automation",
            translation="单人顾问帮本地诊所做「预约提醒自动化」，按月收服务费",
        ),
    ]

    pusher = FeishuPusher("https://open.feishu.cn/open-apis/bot/v2/hook/preview", "blue")
    cap = {}
    async def fake(card):
        cap["c"] = card
        return True
    pusher._send_card = fake

    ok = await pusher.push_opportunities(domestic, international, "2026年08月08日")
    print("push_opportunities 返回:", ok)

    card = cap["c"]
    json.dump(card, open(ROOT / "storage" / "示例卡片-机会挖掘-feishu.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("已写出 storage/示例卡片-机会挖掘-feishu.json")

    # Markdown 预览（中文主标题 + 英文原标题脚注 + 获客优先）
    lines = ["# 模块2 示例 · OPC赚钱机会挖掘（国内 2 + 国际 2）\n",
             "> 规则演示：国际内容用中文翻译做标题，英文原标题降级为脚注；每张卡最该讲透的是「获客」。\n"]
    for region, items in (("🇨🇳 国内", domestic), ("🌍 国际", international)):
        lines.append(f"## {region}\n")
        for it in items:
            head = it.translation or it.title
            lines.append(f"### 💡 {head}")
            if it.translation and it.translation != it.title:
                lines.append(f"> 🌐 英文原标题：{it.title}")
            lines.append(f"- 判断：✅ {it.verdict} ｜ 技术门槛：{it.code_dependency}/5 ｜ 真实性：{it.authenticity}/5 ｜ 综合：{it.relevance_score:.2f}")
            lines.append(f"- 📖 {it.ai_summary}")
            lines.append(f"- 💡 怎么模仿：{it.opportunity_hint}")
            lines.append(f"- 🔧 实操（含获客）：\n{it.practical_steps}")
            lines.append("")
    open(ROOT / "storage" / "示例机会挖掘卡.md", "w", encoding="utf-8").write("\n".join(lines))
    print("已写出 storage/示例机会挖掘卡.md")


if __name__ == "__main__":
    asyncio.run(main())
