"""
反馈闭环 (Feedback Loop)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
先说清楚技术事实（很多人以为飞书按钮天生能回传，其实不是）：

  · 现在这套系统用的是「群自定义机器人 webhook」——它是**单向**的：
    我能往群里发消息，但群里发生了什么（谁点了什么、谁回了什么）我一概收不到。
  · 飞书卡片上的 `callback` 类型按钮确实能回传点击事件，但前提是把机器人
    升级成「飞书自建应用」并配置**事件订阅回调地址**，也就是必须有一台
    24 小时在线、有公网地址的服务器。本项目跑在 GitHub Actions 上（每天
    执行几分钟就关机），没有常驻服务，所以 callback 按钮点了也没人接。

所以这里用的是**不需要服务器、但真的能收到**的方案：

  卡片上放两个 `url` 按钮 → 点击后打开 GitHub「新建 Issue」页面（标题、标签、
  正文里的机会编号全部已预填好）→ 用户只需再点一下绿色的 Submit 按钮。
  第二天 GitHub Actions 跑日报时，先用 GitHub API 把这些 Issue 读回来，
  统计每条机会的 👍/👎，写进机会库，并据此调整后续同类机会的排序。

代价是用户多点一次「提交」；收益是零成本、零运维、反馈永久留档且可回溯。
（如果哪天愿意上一台服务器，把 _build_feedback_actions 换成 callback 按钮即可，
 统计逻辑这一层不用动。）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from library import _norm, _sim, topic_signature

logger = logging.getLogger(__name__)

LIKE_LABEL = "机会反馈-想做"
DISLIKE_LABEL = "机会反馈-没兴趣"

# Issue 正文里埋的机器可读标记
KEY_PREFIX = "机会编号:"
_KEY_RE = re.compile(r"机会编号[:：]\s*([A-Za-z0-9_\u4e00-\u9fff-]+)")


# ============================================================
# 1) 生成反馈按钮的链接（供 push/feishu.py 调用）
# ============================================================

def build_feedback_urls(repo: str, topic_key: str, topic_title: str) -> tuple[str, str]:
    """返回 (想做链接, 没兴趣链接)。repo 形如 'owner/name'。"""
    title_short = (topic_title or "")[:50]
    body = (
        f"{KEY_PREFIX} {topic_key}\n\n"
        f"主题：{title_short}\n\n"
        "（这条 Issue 由日报卡片自动预填，直接点下方绿色按钮提交即可，"
        "无需修改内容。系统次日会自动读取并调整推送权重。）"
    )
    base = f"https://github.com/{repo}/issues/new"
    like = (
        f"{base}?title={quote('[想做] ' + title_short)}"
        f"&labels={quote(LIKE_LABEL)}&body={quote(body)}"
    )
    dislike = (
        f"{base}?title={quote('[没兴趣] ' + title_short)}"
        f"&labels={quote(DISLIKE_LABEL)}&body={quote(body)}"
    )
    return like, dislike


# ============================================================
# 2) 读回反馈（次日运行时）
# ============================================================

class FeedbackCollector:
    """从 GitHub Issues 读回用户对历史机会的 👍/👎。"""

    def __init__(self, repo: str, token: str = "", enabled: bool = True):
        self.repo = (repo or "").strip()
        self.token = (token or "").strip()
        self.enabled = bool(enabled and self.repo and "/" in self.repo)

    async def fetch(self) -> dict[str, dict]:
        """返回 {topic_key: {'up': n, 'down': m}}。失败返回空 dict，不影响主流程。"""
        if not self.enabled:
            return {}
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        tally: dict[str, dict] = {}
        url = f"https://api.github.com/repos/{self.repo}/issues"
        params = {"state": "all", "per_page": 100, "labels": ""}
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as client:
                for label, field in ((LIKE_LABEL, "up"), (DISLIKE_LABEL, "down")):
                    params["labels"] = label
                    resp = await client.get(url, params=params)
                    if resp.status_code == 404:
                        logger.warning("反馈仓库不存在或不可读: %s", self.repo)
                        return {}
                    resp.raise_for_status()
                    for issue in resp.json():
                        if "pull_request" in issue:
                            continue
                        m = _KEY_RE.search(issue.get("body") or "")
                        if not m:
                            continue
                        key = m.group(1)
                        tally.setdefault(key, {"up": 0, "down": 0})
                        tally[key][field] += 1
        except Exception as e:
            logger.warning(f"读取反馈失败（跳过，不影响推送）: {e}")
            return {}

        if tally:
            up = sum(v["up"] for v in tally.values())
            down = sum(v["down"] for v in tally.values())
            logger.info(f"读取到历史反馈：👍 {up} 次 / 👎 {down} 次，覆盖 {len(tally)} 个主题")
        return tally


# ============================================================
# 3) 把反馈变成排序权重（口味学习）
# ============================================================

class PreferenceProfile:
    """
    根据历史 👍/👎 学出「用户口味」，用来微调今天新机会的排序。

    两条通道：
      · 主题相似度：今天这条机会像不像用户点过赞（或点过踩）的老主题；
      · 来源信誉：某个来源反复产出被点赞的内容 → 它的新内容小幅加分。

    调整幅度刻意做得很克制（±1 到 ±2 分），避免一两次点击就把系统带偏。
    """

    SIM_HIT = 0.4  # 与历史主题相似度超过此值才算"同类"

    def __init__(self, boost: int = 1, penalty: int = 2):
        self.boost = int(boost)
        self.penalty = int(penalty)
        self.liked: dict[str, int] = {}      # 归一化指纹 → 反馈次数（置信度）
        self.disliked: dict[str, int] = {}
        self.source_score: dict[str, int] = {}

    @classmethod
    def from_library(cls, library, boost: int = 1, penalty: int = 2) -> "PreferenceProfile":
        p = cls(boost, penalty)
        for ent in (library.entries or {}).values():
            fb = ent.get("feedback") or {}
            up, down = int(fb.get("up", 0)), int(fb.get("down", 0))
            if up <= 0 and down <= 0:
                continue
            fp = ent.get("fingerprint") or _norm(ent.get("topic", ""))
            if up > down:
                p.liked[fp] = p.liked.get(fp, 0) + up
                for s in set(ent.get("sources", [])):
                    p.source_score[s] = p.source_score.get(s, 0) + 1
            elif down > up:
                p.disliked[fp] = p.disliked.get(fp, 0) + down
                for s in set(ent.get("sources", [])):
                    p.source_score[s] = p.source_score.get(s, 0) - 1
        return p

    @property
    def active(self) -> bool:
        return bool(self.liked or self.disliked)

    def adjust(self, items: list) -> list:
        """就地调整 startup_index，并在 score_reason 上追加调整说明。"""
        if not self.active:
            return items
        for it in items:
            _readable, fp = topic_signature(it)
            if not fp:
                continue
            like_sim = max((_sim(fp, x) for x in self.liked), default=0.0)
            dis_sim = max((_sim(fp, x) for x in self.disliked), default=0.0)
            # 置信度：命中同类偏好的累计反馈次数（≥3 次额外 ±1，高置信度加成）
            like_n = max((n for x, n in self.liked.items() if _sim(fp, x) >= self.SIM_HIT), default=0)
            dis_n = max((n for x, n in self.disliked.items() if _sim(fp, x) >= self.SIM_HIT), default=0)
            src = getattr(it, "source_name", "") or getattr(it, "source", "")
            src_pts = self.source_score.get(src, 0)

            delta, why = 0, []
            if like_sim >= self.SIM_HIT:
                delta += self.boost + (1 if like_n >= 3 else 0)
                why.append("与你点过「想做」的主题相似")
            if dis_sim >= self.SIM_HIT:
                delta -= self.penalty + (1 if dis_n >= 3 else 0)
                why.append("与你点过「没兴趣」的主题相似")
            if src_pts >= 2:
                delta += 1
                why.append(f"来源「{src}」你多次点赞")
            elif src_pts <= -2:
                delta -= 1
                why.append(f"来源「{src}」你多次点踩")

            if delta:
                old = getattr(it, "startup_index", 0) or 0
                new = max(1, min(10, old + delta))
                it.startup_index = new
                note = f"根据你的反馈 {old}→{new} 分（{'、'.join(why)}）"
                it.score_reason = (
                    f"{getattr(it, 'score_reason', '')} · {note}"
                    if getattr(it, "score_reason", "")
                    else note
                )
        # 调整后重新排序
        items.sort(
            key=lambda x: (
                getattr(x, "startup_index", 0) or 0,
                getattr(x, "relevance_score", 0) or 0,
            ),
            reverse=True,
        )
        return items
