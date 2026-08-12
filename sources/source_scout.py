"""
内容源侦察兵 (Source Scout)

每天在采集「内容」的同时，主动侦察**新的内容源**（当前主打公众号，platform 字段预留扩展）：
- 那些「写一人公司 / OPC 最牛」的公众号
- 或本身就是「牛逼的一人公司 / 个人IP」的公众号

侦察流程：
1. 用搜狗微信账号搜索（复用 WeixinSearchSource._search）按定向词找出候选公众号
2. 用 LLM 评估每个候选：是否「写一人公司/个人IP」或「本身是一人公司」、质量打分 1-10
3. 通过的写入 storage/scouted_sources.json（动态源注册表），并自动并入白名单，
   次日（甚至当日，由 main 注入 config）即开始被轮询
4. 已评估过的（无论通过/淘汰）在 rejudge_days 内不再重复评估，避免重复烧 token

设计要点：
- 与模块1「发现操盘手」互补：发现引擎找的是「值得拆解的人」，侦察兵找的是
  「值得每天追更的内容源（公众号）」——前者进 roster，后者进白名单注册表。
- 所有网络/LLM 调用都已 try/except 兜底；搜狗被反爬挡住时本轮返回空，不崩溃。
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from filters import is_technical
from typing import Optional

from .base import ContentItem
from .weixin_search import WeixinSearchSource

logger = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))

# 默认侦察词：面向「找账号」而非「找文章」，用「公众号/关注/推荐」类词触发账号搜索
DEFAULT_SCOUT_QUERIES = [
    "一人公司 公众号",
    "副业 公众号 推荐",
    "个人IP 公众号 关注",
    "轻创业 公众号",
    "不上班 公众号 搞钱",
    "知识付费 公众号 推荐",
    "自由职业 公众号 大佬",
    "小本创业 公众号",
]

SCOUT_SYSTEM_PROMPT = """你是一个「内容源策展人」，为一档名为《OPC 赚钱机会挖掘日报》的栏目筛选**每日可以追更的内容源**。

栏目读者是完全不会写代码的人，要的是「能照着抄赚钱作业」——所以好的内容源要么：
- **写一人公司 / OPC 最牛**：持续产出「普通人怎么靠自媒体/内容/社群/服务/信息产品赚钱」的实操复盘；
- **本身就是牛逼的一人公司 / 个人IP**：亲自跑通一门小生意、靠真实交付活着（不是靠教别人赚钱）。

## 你要判定的候选
每条候选给你：账号名 + 从搜索结果里抓到的简介/摘要。你要判断它值不值得加入「每日内容源名单」。

## 排除（verdict=reject）
- 卖课党：收入主要来自「教别人怎么赚钱/做一人公司」，本身不靠真实交付跑通生意（除非他先靠真实业务跑通、卖课只是顺带）
- 写代码/做 SaaS/卖代码模板/搞自动化工作流（n8n、Make、Zapier）的账号——对不会代码的读者没用
- **技术大牛/技术博主/程序员出身、靠技术影响力变现的账号**——读者完全不懂代码，看了也学不会，一律 reject
- 纯泛科技资讯、新闻、融资报道、聚合号（XX日报/资讯）
- 大公司/需团队或融资的事
- 已成名多年的头部大V（读者已错过窗口期，不值得「新挖掘」）

## 打分 (score 1-10)
评估它作为「每日可追更、持续产出优质 OPC 赚钱实操」内容源的质量：
- 题材高度贴合（一人公司/个人IP/轻创业实操）+ 持续更新 + 有真实交付案例 → 9-10
- 部分对题但偏方法论/偶有干货 → 6-8
- 边缘、不确定、可能跑题 → 3-5
- 明显不符/该排除 → 1-2（verdict=reject）

## 输出格式（严格 JSON 数组，不要 markdown 包裹）
- 字符串值里若需引用，一律用中文引号「」或“”包裹；严禁在 JSON 字符串值里使用未转义的双引号，否则 JSON 解析失败、当天侦察归零。
- 字段值不要带尾随逗号；不要输出 ```json 围栏，也不要在 JSON 前后加解释文字。
对每条候选返回：
{
  "name": "账号名（与候选一致，不要翻译）",
  "platform": "weixin",
  "verdict": "add" 或 "reject",
  "score": 8,
  "reason": "一句话：为什么该加入/不该加入（是否写一人公司/本身是一人公司、质量如何）。用中文写。"
}
只返回你评估过的候选；如果候选里没有值得的，返回空数组 []。"""


@dataclass
class ScoutedSource:
    """被侦察兵评估过的一个内容源。"""

    name: str
    platform: str            # weixin / xiaoyuzhou / rss ...
    verdict: str             # add / reject
    score: int               # 1-10 质量分
    reason: str
    discovered_at: str = ""  # 首次评估日期 YYYY-MM-DD

    def to_dict(self) -> dict:
        return asdict(self)


class SourceScout:
    """主动侦察新的 OPC 内容源，并维护动态源注册表。"""

    def __init__(self, dynamic_file: Path, config: dict | None = None):
        self.file = Path(dynamic_file)
        self.cfg = config or {}
        self.queries = self.cfg.get("queries", DEFAULT_SCOUT_QUERIES)
        self.max_candidates = self.cfg.get("max_candidates", 20)
        self.min_score = self.cfg.get("min_score", 7)
        self.rejudge_days = self.cfg.get("rejudge_days", 60)
        self._registry = self._load()

    # ── 注册表持久化 ──────────────────────────────────────────
    def _load(self) -> dict:
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "entries" in data:
                    return data
            except Exception:
                logger.error(f"侦察兵注册表读取失败: {self.file}")
        return {"entries": []}

    def _save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def known_names(self) -> set[str]:
        """已评估过的账号名集合（用于去重）。"""
        return {e.get("name", "") for e in self._registry.get("entries", [])}

    def _is_fresh(self, name: str) -> bool:
        """已评估过且在 rejudge 窗口内 → 跳过，不重复评估。"""
        for e in self._registry.get("entries", []):
            if e.get("name") == name:
                last = e.get("last_judged") or e.get("discovered_at", "")
                if last:
                    try:
                        d = datetime.strptime(last, "%Y-%m-%d")
                        if (datetime.now(CST) - d).days < self.rejudge_days:
                            return True
                    except Exception:
                        return True  # 无法解析也视为已知，跳过
                return True
        return False

    def _upsert(self, ss: ScoutedSource, today: str):
        """写入/更新注册表（同名则更新，并刷新 last_judged）。"""
        entries = self._registry.setdefault("entries", [])
        for e in entries:
            if e.get("name") == ss.name:
                merged = {**ss.to_dict(), "last_judged": today}
                e.clear()
                e.update(merged)
                return
        d = ss.to_dict()
        d["last_judged"] = today
        entries.append(d)

    # ── 候选采集（稳定：优先从已采集内容挖账号名，DDG 主动发现兜底）──────────
    async def gather_candidates(
        self, client, items: list | None = None
    ) -> list[tuple[str, str]]:
        """找出候选公众号，返回 [(账号名, 简介), ...]。

        两条稳定路径（都不依赖被墙的搜狗账号搜索）：
        1) 从「当天已采集到的微信文章」里直接取出发布账号名——这些账号已经真实出现在
           内容流里，是最可靠的候选源（WeWe-RSS / DDG 拉进来的文章都带 公众号·账号名）。
        2) DDG 主动搜索 OPC 主题，抓 mp.weixin 文章页提取公众号名（最佳努力，可能受代理限流）。
        """
        seen: dict[str, str] = {}

        # 路径1：从已采集的微信文章挖账号名
        if items:
            for it in items:
                src = getattr(it, "source", "") or ""
                if src in ("weixin", "weixin_whitelist"):
                    acct = self._account_from_item(it)
                    if acct and 2 <= len(acct) <= 20:
                        desc = f"{getattr(it, 'title', '')}。{getattr(it, 'summary', '')}".strip()
                        seen.setdefault(acct, desc)

        # 路径2：DDG 主动发现新公众号（最佳努力）
        try:
            for a, d in await self._gather_from_ddg(client):
                seen.setdefault(a, d)
        except Exception as e:
            logger.error(f"侦察兵 DDG 主动发现失败（已跳过）: {e}")

        cands = [(a, d) for a, d in seen.items() if not self._is_fresh(a)]
        logger.info(
            f"侦察兵候选: 去重后 {len(cands)} 个公众号"
            f"（已知跳过 {len(seen) - len(cands)}）"
        )
        return cands[: self.max_candidates]

    @staticmethod
    def _account_from_item(it) -> str:
        """从一条微信内容项取出发布账号名。"""
        sn = getattr(it, "source_name", "") or ""
        if sn.startswith("公众号·"):
            return sn.replace("公众号·", "", 1).strip()
        return ""

    async def _gather_from_ddg(self, client) -> list[tuple[str, str]]:
        """DDG 主动搜索 OPC 主题，抓 mp.weixin 文章页提取公众号名。

        每条文章抓一页、用 _extract_nickname 拿账号名；限流/失败则跳过，不阻塞主流程。
        """
        per_q = max(1, self.max_candidates // max(1, len(self.queries)))
        found: dict[str, str] = {}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        }
        for q in self.queries:
            if len(found) >= self.max_candidates:
                break
            try:
                r = await client.post(
                    "https://html.duckduckgo.com/html/", data={"q": q}, timeout=15
                )
            except Exception:
                continue
            if r.status_code not in (200, 202):
                continue
            links = []
            for m in re.finditer(r"uddg=([^&\"]+)", r.text):
                u = unquote(m.group(1).replace("&amp;", "&"))
                if "mp.weixin.qq.com/s/" in u:
                    links.append(u)
            for link in links[:per_q]:
                if len(found) >= self.max_candidates:
                    break
                try:
                    ar = await client.get(link, headers=headers, timeout=15)
                    nick = self._extract_nickname(ar.text)
                except Exception:
                    nick = ""
                if nick and 2 <= len(nick) <= 20:
                    found.setdefault(nick, q)
        return list(found.items())

    @staticmethod
    def _extract_nickname(html: str) -> str:
        """从 mp.weixin 文章页提取公众号名（多种形态兼容）。返回空串表示没取到。"""
        if not html:
            return ""
        patterns = [
            r'var\s+nickname\s*=\s*["\']([^"\']+)["\']',
            r'profile_nickname["\']?\s*>([^<]+)<',
            r'og:article:author"\s+content=["\']([^"\']+)["\']',
            r'"nickname"\s*:\s*["\']([^"\']+)["\']',
            r'<author>([^<]+)</author>',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                name = m.group(1).strip().strip('"').strip()
                if name:
                    return name[:30]
        return ""

    # ── LLM 评估 ─────────────────────────────────────────────
    async def judge(self, ai, candidates: list[tuple[str, str]]) -> list[ScoutedSource]:
        if not candidates:
            return []
        lines = [
            f"[{i}] 账号：{a}\n简介：{d}" for i, (a, d) in enumerate(candidates)
        ]
        user = "\n\n".join(lines)
        raw = await ai.call_llm(
            SCOUT_SYSTEM_PROMPT, user, max_tokens=3000, temperature=0.2
        )
        results = self._parse(raw)
        today = datetime.now(CST).strftime("%Y-%m-%d")
        approved: list[ScoutedSource] = []
        for r in results:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            try:
                score = int(r.get("score", 0) or 0)
            except (TypeError, ValueError):
                score = 0
            verdict = (r.get("verdict") or "reject").strip()
            ss = ScoutedSource(
                name=name,
                platform=(r.get("platform") or "weixin").strip(),
                verdict=verdict,
                score=score,
                reason=(r.get("reason") or "").strip(),
                discovered_at=today,
            )
            # 双保险：读者完全不懂代码，含强技术信号的内容源一律不进白名单
            if is_technical(f"{ss.name} {ss.reason}"):
                logger.info(f"侦察源 [{ss.name}] 命中技术关键词，强制判 reject（不推技术向源）")
                ss.verdict = "reject"
                ss.reason = (ss.reason + "（命中技术关键词，已强制排除）").strip()
            self._upsert(ss, today)
            if ss.verdict == "add" and ss.score >= self.min_score:
                approved.append(ss)
        self._save()
        return approved

    @staticmethod
    def _parse(raw: str) -> list[dict]:
        """解析 LLM 返回的 JSON 数组（兼容 ```json 包裹 / 裸数组 / 单对象 / 未转义引号）。"""
        import jsonfix

        obj = jsonfix.parse_llm_json(raw)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            return [obj]
        return []

    # ── 对外查询 ─────────────────────────────────────────────
    def approved_accounts(self, platform: str = "weixin") -> list[str]:
        """返回通过评估、可作为白名单轮询的账号名。"""
        return [
            e.get("name")
            for e in self._registry.get("entries", [])
            if e.get("verdict") == "add" and e.get("platform") == platform
        ]

    # ── 主入口 ───────────────────────────────────────────────
    async def scout(
        self, ai, client, items: list | None = None
    ) -> list[ScoutedSource]:
        """执行一轮侦察，返回本轮新通过的内容源。

        items：当天已采集到的内容项（可选）。传入后侦察兵优先从中挖账号名，
        不再依赖被墙的搜狗搜索，更稳定。
        """
        cands = await self.gather_candidates(client, items)
        if not cands:
            logger.info("侦察兵：本轮无新候选（可能内容源被限流，属正常降级）")
            return []
        approved = await self.judge(ai, cands)
        logger.info(f"侦察兵：本轮通过 {len(approved)} 个新内容源")
        return approved
