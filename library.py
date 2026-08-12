"""
跨天机会库 (Opportunity Library)

解决的问题：日报是"当天即焚"的——今天推的一条机会，如果一周后另一个来源又讲了
同一件事，用户完全看不出来。而「同一个机会被多个独立来源、在多天里反复讲到」，
恰恰是这条机会真实存在、不是个例的最强信号。

做法：
  1. 给每条机会算一个「主题指纹」（基于「卖给谁 + 卖什么」，退化时用机会提示）；
  2. 用字符二元组 Jaccard 相似度做模糊归并（无需分词库，中英文都能用）；
  3. 持久化到 storage/opportunity_library.json，记录首次出现日期、出现次数、
     印证过它的不同来源、历史最高分；
  4. 回写到 ContentItem，卡片上直接标注「🔁 第3次出现 · 已被2个来源印证」。

副产品：top_recurring() 给出「近 N 天反复出现的主题」，放在日报开头当"本周风向"。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 两条机会的主题指纹相似度 ≥ 此值 → 视为同一个主题（二元组路线）
SIM_THRESHOLD = 0.45

# 归并阈值（分两条路线，避免单字 Jaccard 把不同主题误合并）：
#  · 二元组路线：要求二元组相似度 ≥ BIGRAM_THRESHOLD（抓「连着说的词」，对同义改写较严）
#  · 单字路线：仅在两套文本「几乎由同一批字组成」（单字 Jaccard ≥ UNIGRAM_THRESHOLD，
#    基本是纯语序颠倒）时才放行，杜绝「手工皂 vs 手工蜡烛」「英语陪练 vs 日语陪练」误并。
#  · 单字路线额外要求二元组不低于 BIGRAM_FLOOR——只差一两个字的不同生意
#    （英语陪练 vs 日语陪练，二元组≈0.71）不该因为单字几乎相同就被并掉。
BIGRAM_THRESHOLD = 0.75
UNIGRAM_THRESHOLD = 0.92
BIGRAM_FLOOR = 0.45

# 归并时忽略的高频无意义词（出现在几乎每条机会里，会把不相关主题拉近）
_NOISE = [
    "一人公司", "普通人", "副业", "赚钱", "变现", "月入", "收入", "创业",
    "机会", "项目", "方法", "教你", "如何", "怎么", "分享", "干货",
    "一份", "每份", "每单", "每月", "收费", "仅需", "起步",
    "的", "了", "和", "与", "在", "是", "做", "用",
]

# 金额/数字是噪声：「代写简历199」和「代写简历，一份200」讲的是同一件事，
# 数字留着反而会把两条同主题的机会拉开距离。
_PRICE_RE = re.compile(r"[¥$￥]?\d+(?:\.\d+)?\s*(?:元|块|刀|美元|美金|万|千|[kw])?")

# 同义/缩写归一：语义相同但用词不同的机会归并到同一主题（2026-08-13 聚类增强）
_SYNONYMS = [
    ("搜索引擎优化", "seo"), ("人工智能", "ai"), ("大模型", "ai"),
    ("无代码", "nocode"), ("零代码", "nocode"), ("不写代码", "nocode"),
    ("播客", "podcast"), ("时事通讯", "newsletter"), ("邮件订阅", "newsletter"),
    ("电商", "ecommerce"), ("跨境电商", "ecommerce"),
]


def _norm(text: str) -> str:
    """归一化：先同义/缩写归一，再去金额数字、去噪声词、去标点空白、转小写。"""
    if not text:
        return ""
    s = str(text).lower()
    for full, short in _SYNONYMS:
        s = s.replace(full, short)
    s = _PRICE_RE.sub("", s)
    for w in _NOISE:
        s = s.replace(w, "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def _bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _jaccard(ga: set, gb: set) -> float:
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# 单字 Jaccard 的权重折扣（仅用于 _sim 打分，不再作为归并闸门）。
_UNIGRAM_DISCOUNT = 0.85


def _sim(a: str, b: str) -> float:
    """
    主题相似度（0-1），中英文通吃、零依赖分词。用于挑「最像」的那条做归并，
    但**是否算同一主题由 _is_same_topic 决定**（分两路阈值，见下）。

    返回「字符二元组 Jaccard」与「打折后的单字 Jaccard」的较大值。
    """
    if not a or not b:
        return 0.0
    bi = _jaccard(_bigrams(a), _bigrams(b))
    uni = _jaccard(set(a), set(b))
    return max(bi, uni * _UNIGRAM_DISCOUNT)


def _is_same_topic(fp_a: str, fp_b: str) -> bool:
    """两条归一化指纹是否同一主题：二元组足够相似，或单字近乎完全相同（纯语序颠倒）。

    用「取较大值」会误并：手工皂市集摆摊 vs 手工蜡烛市集摆摊、英语陪练 vs 日语陪练，
    单字 Jaccard 都很高（共享大半字符）却不是同一生意。所以分两路：
      · 二元组 ≥ BIGRAM_THRESHOLD（同一批「连着说的词」）→ 同主题；
      · 否则要求单字 Jaccard ≥ UNIGRAM_THRESHOLD（几乎由同一批字组成，纯语序颠倒）
        → 才认作同主题，避免只差一两个字就误并。
    """
    if not fp_a or not fp_b:
        return False
    bi = _jaccard(_bigrams(fp_a), _bigrams(fp_b))
    if bi >= BIGRAM_THRESHOLD:
        return True
    uni = _jaccard(set(fp_a), set(fp_b))
    return uni >= UNIGRAM_THRESHOLD


def topic_signature(item) -> tuple[str, str]:
    """
    返回 (人类可读主题, 归一化指纹)。

    优先用「卖给谁 + 卖什么」——这是机会的本质；
    copy_template 缺失时退化到机会提示，再退化到标题。
    """
    tpl = getattr(item, "copy_template", None) or {}
    who = (tpl.get("who") or "").strip()
    what = (tpl.get("what") or "").strip()
    if who and what:
        readable = f"{what} → 卖给{who}"
    else:
        readable = (
            (getattr(item, "opportunity_hint", "") or "").strip()
            or (getattr(item, "translation", "") or "").strip()
            or (getattr(item, "title", "") or "").strip()
        )
    return readable[:80], _norm(readable)


class OpportunityLibrary:
    """跨天机会库：归并同主题、累计出现次数与来源印证数。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}

    # ---------------- 持久化 ----------------

    def load(self) -> "OpportunityLibrary":
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.entries = data.get("entries", {}) or {}
            except Exception as e:
                logger.warning(f"机会库读取失败，按空库处理: {e}")
                self.entries = {}
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "entries": self.entries,
        }
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ---------------- 核心：归并 + 标注 ----------------

    def _match(self, fingerprint: str) -> str | None:
        """在已有条目里找相似度最高且过「两路阈值」的主题 key。"""
        best_key, best_sim = None, 0.0
        for key, ent in self.entries.items():
            other = ent.get("fingerprint", "")
            if not _is_same_topic(fingerprint, other):
                continue
            # 仍用 _sim 在「同主题」候选里挑最像的那条，便于归并到最贴切的主题
            s = _sim(fingerprint, other)
            if s > best_sim:
                best_key, best_sim = key, s
        return best_key

    def annotate(self, items: list, today: str | None = None) -> list:
        """
        把机会列表并入库，并把「第几次出现 / 几个来源印证」写回每个 item。
        同一天内同一主题只累加一次 times_seen（避免当天多条同类内容刷次数）。
        """
        today = today or datetime.now(CST).strftime("%Y-%m-%d")
        for it in items:
            readable, fp = topic_signature(it)
            if not fp:
                continue
            key = self._match(fp)
            source = (getattr(it, "source_name", "") or getattr(it, "source", "") or "未知来源")

            if key is None:
                key = f"t{len(self.entries) + 1:04d}_{fp[:12]}"
                ent = {
                    "topic": readable,
                    "fingerprint": fp,
                    "first_seen": today,
                    "last_seen": today,
                    "times_seen": 1,
                    "sources": [source],
                    "urls": [getattr(it, "url", "")],
                    "best_startup_index": getattr(it, "startup_index", 0) or 0,
                    "best_commercial": getattr(it, "commercial_score", 0) or 0,
                    "feedback": {"up": 0, "down": 0},
                }
                self.entries[key] = ent
            else:
                ent = self.entries[key]
                if ent.get("last_seen") != today:
                    ent["times_seen"] = int(ent.get("times_seen", 1)) + 1
                    ent["last_seen"] = today
                srcs = ent.setdefault("sources", [])
                if source not in srcs:
                    srcs.append(source)
                urls = ent.setdefault("urls", [])
                u = getattr(it, "url", "")
                if u and u not in urls:
                    urls.append(u)
                    ent["urls"] = urls[-10:]
                ent["best_startup_index"] = max(
                    int(ent.get("best_startup_index", 0) or 0),
                    getattr(it, "startup_index", 0) or 0,
                )
                ent["best_commercial"] = max(
                    int(ent.get("best_commercial", 0) or 0),
                    getattr(it, "commercial_score", 0) or 0,
                )
                # 主题名用更具体（更长）的那个
                if len(readable) > len(ent.get("topic", "")):
                    ent["topic"] = readable

            it.topic_key = key
            it.repeat_count = int(ent.get("times_seen", 1))
            it.corroborations = len(set(ent.get("sources", [])))
            it.first_seen = ent.get("first_seen", today)

        return items

    # ---------------- 查询 ----------------

    def top_recurring(self, days: int = 7, limit: int = 3, min_times: int = 2) -> list[dict]:
        """近 N 天内反复出现的主题（出现次数 ≥ min_times），按次数+来源数排序。"""
        cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
        pool = [
            {
                "topic": e.get("topic", ""),
                "times": int(e.get("times_seen", 1)),
                "sources": len(set(e.get("sources", []))),
                "first_seen": e.get("first_seen", ""),
                "best_startup_index": int(e.get("best_startup_index", 0) or 0),
            }
            for e in self.entries.values()
            if e.get("last_seen", "") >= cutoff and int(e.get("times_seen", 1)) >= min_times
        ]
        pool.sort(key=lambda x: (x["times"], x["sources"], x["best_startup_index"]), reverse=True)
        return pool[:limit]

    def stats(self) -> str:
        n = len(self.entries)
        repeated = sum(1 for e in self.entries.values() if int(e.get("times_seen", 1)) >= 2)
        return f"机会库 {n} 个主题（其中 {repeated} 个已重复出现）"

    # ---------------- 反馈写入（供 feedback.py 调用）----------------

    def apply_feedback(self, tallies: dict[str, dict]) -> int:
        """把 {topic_key: {'up': n, 'down': m}} 合并进库，返回更新的条目数。"""
        n = 0
        for key, fb in (tallies or {}).items():
            ent = self.entries.get(key)
            if not ent:
                continue
            ent["feedback"] = {
                "up": int(fb.get("up", 0)),
                "down": int(fb.get("down", 0)),
            }
            n += 1
        return n
