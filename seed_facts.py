#!/usr/bin/env python3
"""
种子事实 + 技术门槛分类 应用脚本。

- 把 storage/seeded_facts.json 里的研究结论合并进 storage/operators.json
- 支持「新建」操盘手（entry 带 name+region 且名单里没有 → 自动创建）
- 给每个操盘手打 tech_barrier（无/低/中/高）
- 名单里未被任何 entry 标注 tech_barrier 的「现有关键人」（多为 indie hacker），
  默认判为「高」并被系统过滤，避免给不会写代码的用户推需开发软件的人

用法:
  python seed_facts.py            # 手动重建名单（合并事实 + 分类）
在 main.py 中: from seed_facts import apply_seeds; apply_seeds(roster, SEEDS_PATH)
"""

import json
import logging
from pathlib import Path

from operators import Operator, OperatorRoster

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
SEEDS_PATH = ROOT / "storage" / "seeded_facts.json"
ROSTER_PATH = ROOT / "storage" / "operators.json"


def apply_seeds(roster: OperatorRoster, seeds_path: Path = SEEDS_PATH) -> dict:
    """把 seeded_facts.json 合并进 roster（就地修改）。返回统计。"""
    data = json.loads(seeds_path.read_text(encoding="utf-8"))
    entries = data.get("entries", {})

    merged = 0
    created = 0
    classified = 0

    for key, entry in entries.items():
        if key.startswith("_"):
            continue
        op = roster.operators.get(key)
        if op is None:
            # 新建：entry 必须带 name + region
            name = entry.get("name")
            region = entry.get("region")
            if not name or not region:
                logger.warning(f"跳过无法识别的 entry: {key}")
                continue
            op = Operator(
                handle=key,
                name=name,
                region=region,
                aliases=entry.get("aliases", [name]),
                sources=entry.get("sources", ["research"]),
                category=entry.get("category", ""),
                tech_barrier=entry.get("tech_barrier", ""),
            )
            roster.operators[key] = op
            created += 1
            logger.info(f"新建操盘手: {name} ({region})")

        # 合并种子事实
        facts = entry.get("seeded_facts")
        if facts:
            op.seeded_facts.update(facts)
            merged += 1
        # 覆盖分类字段
        if entry.get("tech_barrier"):
            op.tech_barrier = entry["tech_barrier"]
            classified += 1
        if entry.get("category"):
            op.category = entry["category"]

    # 未标注技术门槛的现有关键人（多为写代码的 indie hacker）→ 默认判「高」，被系统过滤
    defaulted = 0
    for op in roster.operators.values():
        if not op.tech_barrier:
            op.tech_barrier = "高"
            defaulted += 1

    # 非「发现引擎」来源的操盘手（config 盯人列表 + 研究种子里的人）大多已是成名人物，
    # 不算「新机会」——默认标记为 established=True，使其不进入每日拆解推送。
    # 真正的新机会来自 discovery 引擎（source 含 "discovery"），保持 established=False。
    marked_est = 0
    for op in roster.operators.values():
        if "discovery" not in op.sources and not op.established:
            op.established = True
            marked_est += 1

    stats = {
        "merged": merged, "created": created, "classified": classified,
        "defaulted_high": defaulted, "marked_established": marked_est,
    }
    logger.info(f"种子应用完成: {stats}")
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    # 先从 config 构建（保留 discovery 新增 + 历史拆解），再套用种子
    import yaml
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    roster = OperatorRoster.build_from_config(cfg, ROSTER_PATH)
    stats = apply_seeds(roster)
    roster.save()

    from collections import Counter
    tb = Counter(o.tech_barrier for o in roster.operators.values())
    est = Counter(o.established for o in roster.operators.values())
    print(f"名单总数: {len(roster.operators)}")
    print(f"技术门槛分布: {dict(tb)}")
    print(f"成名(established)分布: {dict(est)}")
    print(f"本次: 合并事实 {stats['merged']} · 新建 {stats['created']} · 标注 {stats['classified']} · 默认高 {stats['defaulted_high']} · 标记成名 {stats['marked_established']}")


if __name__ == "__main__":
    main()
