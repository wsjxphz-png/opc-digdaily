"""
操盘手档案系统 (Operator Roster + Dossier)

职责：
1. 从 config 的 twitter / youtube / 个人 RSS 自动构建「操盘手名单」(roster)
   —— 区分「个人操盘手」与「媒体源」(媒体源仅用于发现，不入名单)
2. 按 source_name 把抓到的内容归档到对应操盘手的 signals
3. 持久化到 storage/operators.json (新增的操盘手 / 历史拆解 都会保留)
4. 轮转挑选「今日待拆解」的操盘手
5. 支持发现引擎把新操盘手加入名单
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 技术门槛：对「完全不会写代码的人」的要求
#   无 = 完全不用碰代码/软件
#   低 = 可用 ChatGPT / Notion / 表单 等现成无代码工具
#   中 = 需配置较复杂工具或有一点技术
#   高 = 必须自己写代码 / 开发软件（本系统默认排除）
TECH_BARRIER_LABEL = {
    "无": "🟢 无需代码",
    "低": "🟢 可用无代码工具",
    "中": "🟡 需配工具/半技术",
    "高": "🔴 需写代码/开发",
}

# 「是否已成名」维度：本系统聚焦「机会」——新的一人公司 / 新 IP / 刚跑通一人模式的人
#   established=True  = 已经红了很久的成名大V/头部博主/知名作家（不是新机会，默认不推送）
#   established=False = 新晋 / 刚起步 / 刚被发现的操盘手（这才是「机会」，优先推送）
# 配置里的「盯人列表」和研究种子里的人大多是已成名人物，统一标记为 established=True；
# 由发现引擎新扫到的操盘手才是新鲜机会，established=False。


# ── 国内 Twitter handle 集合（其余视为国际）──────────────────────
CN_TWITTER_HANDLES = {
    "dotey", "xiaohuggg", "op7418", "khazix0918", "lijigang", "XDash",
    "tualatrix", "oran_ge", "yupi996", "Gorden_sun", "pandatalk8",
    "servasyy_ai", "lyc_zh", "dontbesilent", "imwsl90", "iamtonyzhu",
    "indie_maker_fox", "thinkingjimmy", "yangyi",
}

# ── 个人操盘手 RSS（与媒体源区分；媒体源仅用于发现，不入名单）────
PERSONAL_RSS_NAMES = {
    "Nick Saraev Blog",
    "Daniel Vassallo (Small Bets)",
    "Justin Welsh",
    "Steph Smith",
    "Solo AI Lab (Practical AI)",
    "Cory Zue (产品化服务复盘)",
    "Tony Dinh (独立开发者实战)",
    "Arvid Kahl (The Bootstrapped Founder)",
    "David Perell (写作变现/内容系统)",
    "Dickie Bush (Ship 30 写作系统)",
    "Vista 一人公司/AI 实战（Substack 中文）",
}


class Operator:
    """单个操盘手档案。"""

    def __init__(
        self,
        handle: str,
        name: str,
        region: str,
        aliases: list[str],
        sources: list[str],
        category: str = "",
        tech_barrier: str = "",
        established: bool = False,
    ):
        self.handle = handle            # 唯一 ID：@twitter / youtube_channel_id / rss_feed_name
        self.name = name                # 展示名
        self.region = region            # 国内 / 国际
        self.category = category        # 自动化服务 / 产品化服务 / 内容资产 / 微型工具 ...
        self.tech_barrier = tech_barrier  # 技术门槛：无 / 低 / 中 / 高
        self.aliases = aliases          # 用于归档匹配的 source_name 列表
        self.sources = sources          # twitter / youtube / rss
        self.seeded_facts: dict = {}    # 联网补搜得到的种子事实（定价/MRR/获客/工具链）
        self.signals: list[dict] = []   # 抓到的该人内容 [{title,url,date,summary}]
        self.teardown: Optional[dict] = None
        self.last_teardown: Optional[str] = None
        self.teardown_count: int = 0
        self.discovered_date: str = datetime.now(CST).strftime("%Y-%m-%d")
        self.is_new: bool = False       # 本次运行新发现（用于预警）
        self.established: bool = established  # 是否已成名多年（True=成名大V，不推送；False=新机会，优先推）
        self.last_revisit: Optional[str] = None   # 上次「动态更新」补充的日期
        self.revisit_count: int = 0               # 被动态更新补充的次数
        self.recommended_at: Optional[str] = None # 被「发现引擎」推荐（加入名单）的日期，用于补拆排队
        self.commercial_health: dict = None       # dbs 商业底层逻辑体检结果（compute 返回 dict + gate_reason）
        self.commercial_severity: str = ""        # 分档：ok / warn / skip（skip=结构性坏案例，轮转直接不推）

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "name": self.name,
            "region": self.region,
            "category": self.category,
            "aliases": self.aliases,
            "sources": self.sources,
            "tech_barrier": self.tech_barrier,
            "seeded_facts": self.seeded_facts,
            "signals": self.signals,
            "teardown": self.teardown,
            "last_teardown": self.last_teardown,
            "teardown_count": self.teardown_count,
            "discovered_date": self.discovered_date,
            "is_new": self.is_new,
            "established": self.established,
            "last_revisit": self.last_revisit,
            "revisit_count": self.revisit_count,
            "recommended_at": self.recommended_at,
            "commercial_health": self.commercial_health,
            "commercial_severity": self.commercial_severity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Operator":
        op = cls(
            handle=d.get("handle", ""),
            name=d.get("name", ""),
            region=d.get("region", "国际"),
            aliases=d.get("aliases", []),
            sources=d.get("sources", []),
            category=d.get("category", ""),
            tech_barrier=d.get("tech_barrier", ""),
        )
        op.seeded_facts = d.get("seeded_facts", {}) or {}
        op.signals = d.get("signals", []) or []
        op.teardown = d.get("teardown")
        op.last_teardown = d.get("last_teardown")
        op.teardown_count = d.get("teardown_count", 0) or 0
        op.discovered_date = d.get("discovered_date", "")
        op.is_new = d.get("is_new", False)
        op.established = d.get("established", False) or False
        op.last_revisit = d.get("last_revisit")
        op.revisit_count = d.get("revisit_count", 0) or 0
        op.recommended_at = d.get("recommended_at")
        op.commercial_health = d.get("commercial_health")
        op.commercial_severity = d.get("commercial_severity", "") or ""
        return op


class OperatorRoster:
    """操盘手名单 + 档案持久化。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.operators: dict[str, Operator] = {}

    # ── 持久化 ────────────────────────────────────────────────
    def load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for h, d in data.get("operators", {}).items():
                    self.operators[h] = Operator.from_dict(d)
            except Exception as e:
                logger.error(f"名单加载失败: {e}")
        # 清空本次运行标记
        for op in self.operators.values():
            op.is_new = False
        logger.info(f"名单加载完成: {len(self.operators)} 人")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"operators": {h: o.to_dict() for h, o in self.operators.items()}}
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 从 config 自动构建（保留已发现/已拆解的人）──────────────
    @classmethod
    def build_from_config(cls, config: dict, path: Path) -> "OperatorRoster":
        roster = cls(path)
        roster.load()  # 先加载已有的（含 discovery 新增 + 历史拆解）

        intl = config.get("international", {})
        dom = config.get("domestic", {})
        added = 0

        # ▸ Twitter 账号 → 操盘手
        tw = (
            intl.get("sources", {})
            .get("twitter", {})
            .get("nitter_rss", {})
            .get("accounts", [])
        )
        for acc in tw:
            handle = f"@{acc}"
            if handle in roster.operators:
                continue
            region = "国内" if acc in CN_TWITTER_HANDLES else "国际"
            roster.operators[handle] = Operator(
                handle, acc, region, [handle], ["twitter"]
            )
            added += 1

        # ▸ YouTube 频道 → 操盘手
        yt = (
            intl.get("sources", {})
            .get("youtube", {})
            .get("rss_feeds", {})
            .get("channel_ids", [])
        )
        for ch in yt:
            cid = ch.get("id", "")
            if not cid or cid in roster.operators:
                continue
            label = ch.get("label", cid)
            roster.operators[cid] = Operator(cid, label, "国际", [label], ["youtube"])
            added += 1

        # ▸ 个人 RSS → 操盘手（国际）
        intl_rss = intl.get("sources", {}).get("rss", {}).get("feeds", [])
        for f in intl_rss:
            name = f.get("name", "")
            if name in PERSONAL_RSS_NAMES and name not in [
                o.name for o in roster.operators.values()
            ]:
                roster.operators[name] = Operator(name, name, "国际", [name], ["rss"])
                added += 1

        # ▸ 个人 RSS → 操盘手（国内）
        dom_rss = dom.get("sources", {}).get("rss", {}).get("feeds", [])
        for f in dom_rss:
            name = f.get("name", "")
            if name in PERSONAL_RSS_NAMES and name not in [
                o.name for o in roster.operators.values()
            ]:
                roster.operators[name] = Operator(name, name, "国内", [name], ["rss"])
                added += 1

        if added:
            logger.info(f"从配置新增 {added} 名操盘手到名单")
            roster.save()
        return roster

    # ── 归档：把内容信号归入对应操盘手 ────────────────────────
    def accumulate(self, item) -> bool:
        """把一条内容归档到匹配的操盘手。返回是否命中。"""
        sn = getattr(item, "source_name", "") or ""
        if not sn:
            return False
        for op in self.operators.values():
            if sn in op.aliases:
                sig = {
                    "title": (item.title or "")[:120],
                    "url": item.url or "",
                    "date": (
                        item.published.strftime("%Y-%m-%d")
                        if item.published else ""
                    ),
                    "summary": ((item.full_text or item.summary) or "")[:400],
                }
                if not any(s.get("url") == sig["url"] for s in op.signals):
                    op.signals.append(sig)
                    op.signals = op.signals[-20:]  # 仅保留最近 20 条
                return True
        return False

    # ── 轮转：挑选今日待拆解对象 ──────────────────────────────
    def get_due_for_teardown(
        self, n: int, require_signals: bool = False,
        allowed_tech_barrier: list[str] | None = None,
        exclude_established: bool = True,
    ) -> list[Operator]:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        candidates = [
            op for op in self.operators.values() if op.last_teardown != today
        ]

        # 只保留非技术门槛（无 / 低）的操盘手，过滤掉需写代码的 indie hacker
        if allowed_tech_barrier is not None:
            candidates = [
                c for c in candidates if c.tech_barrier in allowed_tech_barrier
            ]

        # 聚焦「机会」：排除已成名多年的大V（用户要的是新的一人公司 / 新 IP / 刚跑通一人模式的人）
        if exclude_established:
            candidates = [c for c in candidates if not c.established]

        # dbs 商业底层逻辑体检：结构性坏案例（skip）直接不进待拆池，保护初学者
        candidates = [c for c in candidates if c.commercial_severity != "skip"]

        def sort_key(o: Operator):
            # 优先拆解「还没拆过」的人（补拆：过去推荐过但没拆的，先补上），
            # 再按「越早被推荐」优先（推荐队列先进先出，避免积压），
            # 然后「刚被发现/新晋」的人（新机会排前面），最后用新鲜信号/近期未拆做微调。
            # 插入「商业体检分档」：健康(ok)优先、存疑(warn)排后——存疑案例仍推，但靠后。
            never_torn = 0 if o.teardown_count == 0 else 1
            sev = o.commercial_severity or "ok"
            sev_rank = 1 if sev == "warn" else 0
            rec = o.recommended_at or "9999-99-99"   # 越早被推荐越优先补拆
            fresh = o.discovered_date or "0000-00-00"  # 字典序：日期越大越新 → 排前面
            has_sig = 0 if o.signals else 1
            recency = o.last_teardown or "0000-00-00"
            return (never_torn, sev_rank, rec, fresh, has_sig, recency)

        candidates.sort(key=sort_key)
        if require_signals:
            candidates = [c for c in candidates if c.signals]
        return candidates[:n]

    # ── 复盘更新：挑选「值得动态补充」的已拆解操盘手 ──────────────
    def get_due_for_revisit(
        self, n: int, interval_days: int = 14,
        allowed_tech_barrier: list[str] | None = None,
    ) -> list["Operator"]:
        """挑选「值得动态更新补充」的已拆解操盘手：
        - 已拆解过（teardown_count > 0）
        - 非成名大V（与模块1聚焦「新机会」一致）
        - 距上次拆解/更新已 ≥ interval_days
        - 期间出现了新信号（说明他有了新动作 / 新业务）
        """
        candidates = [
            op for op in self.operators.values()
            if op.teardown_count > 0
            and not op.established
            and self._days_since(op.last_revisit or op.last_teardown) >= interval_days
            and self._has_new_signals_since(op, op.last_revisit or op.last_teardown)
        ]
        if allowed_tech_barrier is not None:
            candidates = [c for c in candidates if c.tech_barrier in allowed_tech_barrier]
        # 越久没更新、越该补 → 排前面
        candidates.sort(key=lambda o: (o.last_revisit or o.last_teardown or "0000-00-00",))
        return candidates[:n]

    @staticmethod
    def _days_since(date_str: Optional[str]) -> int:
        if not date_str:
            return 10**9
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return 10**9
        # date_str 是「日」级字符串（naive），用 naive now 比较，避免与 CST aware 时间相减报错
        return (datetime.now() - d).days

    @staticmethod
    def _has_new_signals_since(op: "Operator", since: Optional[str]) -> bool:
        """判断自 since 之后是否出现了新信号（新文章/新动态）。"""
        if not since:
            return bool(op.signals)
        for s in op.signals:
            sd = (s.get("date") or "").strip()
            if sd and sd > since:
                return True
        return False

    # ── 发现：加入新操盘手 ────────────────────────────────────
    def add_operator(
        self,
        handle: str,
        name: str,
        region: str,
        aliases: list[str],
        sources: list[str],
        highlight: str = "",
        tech_barrier: str = "",
    ) -> bool:
        if handle in self.operators:
            return False
        op = Operator(
            handle, name, region, aliases, sources,
            category=highlight, tech_barrier=tech_barrier,
        )
        op.is_new = True
        op.recommended_at = datetime.now(CST).strftime("%Y-%m-%d")
        self.operators[handle] = op
        return True

    def stats(self) -> dict:
        regions = {}
        for op in self.operators.values():
            regions[op.region] = regions.get(op.region, 0) + 1
        torn = sum(1 for op in self.operators.values() if op.teardown_count > 0)
        return {
            "total": len(self.operators),
            "by_region": regions,
            "torn_down": torn,
        }
