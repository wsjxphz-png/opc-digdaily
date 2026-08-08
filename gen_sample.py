#!/usr/bin/env python3
"""
生成「示例拆解卡」的飞书卡片 JSON（仅本地渲染，不推送）。
目的：用真实代码路径（push/feishu._build_teardown_section）证明拆解引擎
产出的结构能被正确渲染成飞书卡片，作为每日推送格式的可视化预览。
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
import sys
sys.path.insert(0, str(ROOT))

from push import FeishuPusher

# 手工构造一条「拆解引擎本应产出」的结构化结果（字段同 teardown.Teardown）
teardown = {
    "operator_handle": "@nicksaraev",
    "operator_name": "Nick Saraev",
    "region": "国际",
    "who": "加拿大人，完全自学、不靠写代码团队赚钱。早期做 LeftClick（单人 AI 自动化机构），现在同时卖工作流模板和付费社区。真人交付型标杆，不是卖课党。",
    "deliverable": "帮中小企业用 n8n/Make 把重复业务流程（线索进表、邮件自动分类、工单流转）搭成自动化工作流；额外卖现成工作流模板 + $184/月 社区 Maker School。",
    "business_model": "接单：单个工作流 $2K–10K 或按月维护；模板：Gumroad 一次性卖；社区：$184/月。公开口径 LeftClick 单人月营收约 $72K。",
    "acquisition": "YouTube 发『用 n8n 帮 X 行业省 Y 小时』实录；Upwork/reddit 接小单做案例；长文复盘 + 冷外联；免费小诊断换前 3 个客户。",
    "stack": "n8n / Make（拖拽搭流程）+ Stripe（收款）+ Gumroad（卖模板）+ YouTube（获客）+ 现成 AI API。零自研代码。",
    "replicability": 4,
    "first_step": "用 n8n 免费版搭一个『表单→自动写进表格并邮件通知』的小流程，录屏发 YouTube/小红书，标题写《我是怎么帮 X 行业省下每天 2 小时》，用免费小改换前 3 个客户。",
    "red_flag": "本人真交付型，主要收入来自帮企业做自动化，不是卖铲子。但 Maker School 社区属『教别人做』，别只学不做。最大坑：光看教程不接单。先接 3 个真客户再谈规模化。",
    "learn": "把自动化做成可反复卖的模板 + 用公开构建内容当免费广告。普通人不必做到 $72K/月，先做到：一个行业 + 一条流程 + 三个月费客户。",
    "signals_used": 6,
    "generated_at": "2026-08-07 预览",
}

pusher = FeishuPusher(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/demo", card_color="blue")

# 用真实渲染函数生成卡片元素
elements = []
elements.append({
    "tag": "div",
    "text": {"tag": "lark_md", "content": "**📑 今日拆解 1 人  ·  🆕 新发现 0 人**"},
})
elements.append({"tag": "hr"})
elements.extend(pusher._build_teardown_section(teardown))

card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "🔍 OPC赚钱机会挖掘日报 · 操盘手拆解 | 2026年08月07日"},
        "template": pusher.card_color,
    },
    "elements": elements,
}

out = ROOT / "storage" / "示例卡片-feishu.json"
out.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已生成飞书卡片 JSON：{out}")
print(f"卡片元素数：{len(card['elements'])}")
