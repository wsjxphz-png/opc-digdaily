"""
X (Twitter) 信息源 — 主通道：X 私有 GraphQL 接口（用登录 Cookie，免费、不需要 API Key）。

为什么不用 Nitter：公共 Nitter 实例 2024 年后基本全部关停，RSS 地址长期 404。
为什么不用官方 API v2：读取用户时间线已进付费档，免费额度取不到内容。

本通道的做法（与 ai-radar 项目同源，已长期稳定跑通）：
  1. 用网页版 X 自带的公开 Bearer（所有浏览器访问 x.com 时用的都是这一个，非私密）；
  2. 带上你自己账号的 auth_token + ct0 两个 Cookie 完成鉴权；
  3. 调 UserByScreenName 拿到 rest_id，再调 UserTweets 拿时间线。

配置：把 Cookie 放到环境变量 TWITTER_COOKIES，值是一段 JSON：
  {"auth_token":"xxxxx","ct0":"yyyyy"}
本地放 .env，线上放 GitHub Secrets。取法：浏览器登录 x.com → F12 →
应用/Application → Cookie → 复制 auth_token 和 ct0 两个值。
⚠️ 这两个值等同于你的登录态，只放 .env 和 GitHub 加密 Secret，绝不要提交进仓库。

未配置 Cookie 时本源自动静默跳过，不影响其它信息源。
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .base import BaseSource, ContentItem

logger = logging.getLogger(__name__)

# ── X 网页客户端公开 Bearer（浏览器打开 x.com 时用的就是它，非私人凭证）──
TWITTER_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
TWITTER_API_BASE = "https://x.com/i/api/graphql"
# ⚠️ X 会定期轮换 GraphQL query ID（前端 main bundle 里的 queryId 映射表）。
# 失效表现：UserByScreenName 仍能返回 uid，但 UserTweets 返回 {"result":{"__typename":"UserUnavailable"}} → 0 条。
# 更新方法：抓 https://abs.twimg.com/responsive-web/client-web/main.*.js，grep `operationName:"UserTweets"` 前的 queryId。
# 当前值抓取于 2026-08-12（main.f3d2f4ca.js）
Q_USER_BY_SCREEN_NAME = "Gb-d6r0vxPOADdG62OEBpQ"
Q_USER_TWEETS = "SXVCYB8XHSS25nzIljNtZA"
TIMEOUT = 30

# sync 版 API 调用（供 asyncio.to_thread 使用，解决 httpx cookie 兼容问题）
def _sync_user_id(session, handle: str):
    """通过 screen_name 获取 Twitter 用户 ID（同步版，用 requests.Session）。"""
    variables = json.dumps({"screen_name": handle}, separators=(",", ":"))
    url = f"{TWITTER_API_BASE}/{Q_USER_BY_SCREEN_NAME}/UserByScreenName?variables={variables}&features={TWITTER_FEATURES}"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("user", {}).get("result", {}).get("rest_id")
    except Exception:
        pass
    return None


def _sync_user_tweets(session, user_id: str, count: int = 30) -> list:
    """获取用户推文列表（同步版，用 requests.Session）。"""
    variables = json.dumps(
        {"userId": user_id, "count": count, "includePromotedContent": False,
         "withQuickPromoteEligibilityTweetFields": False, "withVoice": False,
         "withV2Timeline": True},
        separators=(",", ":"),
    )
    url = f"{TWITTER_API_BASE}/{Q_USER_TWEETS}/UserTweets?variables={variables}&features={TWITTER_FEATURES}"
    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        # X UserTweets 返回结构是 data.user.result.timeline.timeline（无 timeline_v2 层）
        tl = (data.get("data", {}).get("user", {}).get("result", {})
              .get("timeline", {}).get("timeline", {}))
        tweets = []
        for inst in tl.get("instructions", []):
            if inst.get("type") == "TimelineAddEntries":
                for entry in inst.get("entries", []):
                    tw = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                    if tw.get("__typename") == "Tweet" and "legacy" in tw:
                        tweets.append(tw)
        return tweets
    except Exception:
        return []

# GraphQL 的 features 开关必须逐字匹配，缺一个就整体报错
TWITTER_FEATURES = json.dumps(
    {
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_media_download_video_enabled": False,
        "responsive_web_enhance_cards_enabled": False,
    },
    separators=(",", ":"),
)


class TwitterSource(BaseSource):
    name = "twitter"

    async def fetch(self, cfg: dict, keywords: dict) -> list[ContentItem]:
        items: list[ContentItem] = []

        # 主通道：X GraphQL（Cookie 鉴权）
        gql = cfg.get("graphql", {})
        if gql.get("enabled", True):
            items.extend(await self._fetch_graphql(gql, cfg, keywords))

        # 兜底通道：Nitter RSS（公共实例基本全挂，默认关闭）
        nitter = cfg.get("nitter_rss", {})
        if nitter.get("enabled"):
            items.extend(await self._fetch_nitter(nitter, keywords))

        # 兜底通道：官方 API v2（需付费 Key）
        bearer = cfg.get("bearer_token", "")
        if bearer and not bearer.startswith("YOUR_"):
            items.extend(await self._fetch_api(cfg, bearer, keywords))

        logger.info(f"Twitter: 获取到 {len(items)} 条")
        return items

    # ================================================================
    # 主通道：X GraphQL
    # ================================================================

    @staticmethod
    def _accounts(gql: dict, cfg: dict) -> list[str]:
        """优先用 graphql.accounts（精选非技术号）；没配就退回主名单。"""
        accs = gql.get("accounts") or cfg.get("accounts") or []
        if not accs:
            accs = (cfg.get("nitter_rss", {}) or {}).get("accounts", [])
        return [a for a in accs if a]

    async def _fetch_graphql(
        self, gql: dict, cfg: dict, keywords: dict
    ) -> list[ContentItem]:
        cookies_json = os.environ.get("TWITTER_COOKIES", "").strip()
        if not cookies_json:
            logger.info("Twitter: 未配置 TWITTER_COOKIES，跳过 X 抓取（不影响其它源）")
            return []
        try:
            cookies = json.loads(cookies_json)
        except Exception as e:
            logger.warning(f"Twitter: TWITTER_COOKIES 不是合法 JSON，已跳过: {e}")
            return []

        auth_token = (cookies.get("auth_token") or "").strip()
        ct0 = (cookies.get("ct0") or "").strip()
        if not auth_token or not ct0:
            logger.warning("Twitter: TWITTER_COOKIES 缺少 auth_token 或 ct0，已跳过")
            return []

        accounts = self._accounts(gql, cfg)
        if not accounts:
            return []

        hours = int(gql.get("hours", 48))
        per_account_fetch = int(gql.get("tweets_per_account", 30))
        max_keep = int(gql.get("max_per_account", 5))
        min_chars = int(gql.get("min_chars", 80))
        gap = float(gql.get("sleep", 0.8))
        skip_replies = bool(gql.get("skip_replies", True))
        skip_retweets = bool(gql.get("skip_retweets", True))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Authorization": f"Bearer {TWITTER_BEARER}",
            "X-Csrf-Token": ct0,
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Client-Language": "en",
            "Accept": "*/*",
        }

        items: list[ContentItem] = []
        ok = 0
        # httpx 的 cookie 处理在 GraphQL API 上有兼容问题（requests.Session 正常），
        # 用 requests.Session + asyncio.to_thread 替代
        import requests as sync_http
        session = sync_http.Session()
        session.cookies.set("auth_token", auth_token)
        session.cookies.set("ct0", ct0)
        session.headers.update({
            "User-Agent": headers["User-Agent"],
            "Authorization": headers["Authorization"],
            "X-Csrf-Token": ct0,
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
        })

        for handle in accounts:
            try:
                user_id = await asyncio.to_thread(
                    _sync_user_id, session, handle
                )
                if not user_id:
                    logger.debug(f"Twitter @{handle}: 找不到用户（可能改名或被封）")
                    continue
                tweets = await asyncio.to_thread(
                    _sync_user_tweets, session, user_id, per_account_fetch
                )

                kept = 0
                for tw in tweets:
                    if kept >= max_keep:
                        break
                    legacy = tw.get("legacy", {}) or {}
                    created = legacy.get("created_at", "")
                    if not created:
                        continue
                    try:
                        pub = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
                    except Exception:
                        continue
                    if pub < cutoff:
                        continue

                    text = (legacy.get("full_text") or "").strip()
                    if not text or len(text) < min_chars:
                        continue
                    if skip_retweets and text.startswith("RT @"):
                        continue
                    if skip_replies and legacy.get("in_reply_to_status_id_str"):
                        continue

                    tid = tw.get("rest_id", "") or legacy.get("id_str", "")
                    url = f"https://x.com/{handle}/status/{tid}"
                    score = self.keyword_score(text, keywords)
                    eng = (
                        int(legacy.get("favorite_count") or 0)
                        + int(legacy.get("retweet_count") or 0)
                        + int(legacy.get("reply_count") or 0)
                    )
                    items.append(
                        ContentItem(
                            title=text[:150],
                            url=url,
                            summary=text[:1000],
                            full_text=text,
                            source="twitter",
                            source_name=f"@{handle}",
                            published=pub,
                            relevance_score=score,
                            engagement=eng,
                        )
                    )
                    kept += 1
                ok += 1
                if kept:
                    logger.debug(f"Twitter @{handle}: {kept} 条")
            except Exception as e:
                logger.warning(f"Twitter @{handle} 失败: {str(e)[:100]}")
            await asyncio.sleep(gap)

        logger.info(f"Twitter GraphQL: {ok}/{len(accounts)} 个账号成功 → {len(items)} 条")
        return items

    @staticmethod
    async def _user_id(client: httpx.AsyncClient, handle: str):
        variables = json.dumps({"screen_name": handle}, separators=(",", ":"))
        url = (
            f"{TWITTER_API_BASE}/{Q_USER_BY_SCREEN_NAME}/UserByScreenName"
            f"?variables={variables}&features={TWITTER_FEATURES}"
        )
        resp = await client.get(url)
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                logger.warning(
                    "Twitter 鉴权失败(%s)：TWITTER_COOKIES 可能已过期，请重新复制 auth_token / ct0",
                    resp.status_code,
                )
            return None
        data = resp.json()
        return (
            data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("rest_id")
        )

    @staticmethod
    async def _user_tweets(
        client: httpx.AsyncClient, user_id: str, count: int = 30
    ) -> list[dict]:
        variables = json.dumps(
            {
                "userId": user_id,
                "count": count,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": False,
                "withVoice": False,
                "withV2Timeline": True,
            },
            separators=(",", ":"),
        )
        url = (
            f"{TWITTER_API_BASE}/{Q_USER_TWEETS}/UserTweets"
            f"?variables={variables}&features={TWITTER_FEATURES}"
        )
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        data = resp.json()
        timeline = (
            data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
        )
        tweets = []
        for inst in timeline.get("instructions", []):
            if inst.get("type") != "TimelineAddEntries":
                continue
            for entry in inst.get("entries", []):
                result = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                if result.get("__typename") == "Tweet" and "legacy" in result:
                    tweets.append(result)
        return tweets

    # ================================================================
    # 兜底通道
    # ================================================================

    async def _fetch_nitter(
        self, nitter: dict, keywords: list[str]
    ) -> list[ContentItem]:
        """通过 Nitter 实例的 RSS feed 获取推文（公共实例多已关停，仅作兜底）。"""
        instance = nitter.get("instance", "https://nitter.net").rstrip("/")
        accounts = nitter.get("accounts", [])
        items: list[ContentItem] = []
        loop = asyncio.get_event_loop()

        for account in accounts:
            rss_url = f"{instance}/{account}/rss"
            try:
                raw = await loop.run_in_executor(None, feedparser.parse, rss_url)
                for entry in raw.entries[:5]:
                    title = entry.get("title", "")
                    link = entry.get("link", "")
                    summary = entry.get("summary", entry.get("description", ""))
                    summary = self._strip_html(summary)[:300]

                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        from time import mktime
                        ts = mktime(entry.published_parsed)
                        published = datetime.fromtimestamp(ts, tz=timezone.utc)

                    full_text = f"{title}\n{summary}"
                    score = self.keyword_score(full_text, keywords)

                    items.append(ContentItem(
                        title=title,
                        url=link,
                        summary=summary,
                        source="twitter",
                        source_name=f"@{account}",
                        published=published,
                        relevance_score=score,
                    ))
            except Exception as e:
                logger.error(f"Nitter RSS @{account} 获取失败: {e}")

        return items

    async def _fetch_api(
        self, cfg: dict, bearer_token: str, keywords: list[str]
    ) -> list[ContentItem]:
        """通过 Twitter API v2 获取推文 (需要付费 tier)。"""
        accounts = cfg.get("accounts", [])
        if not accounts:
            return []

        items: list[ContentItem] = []
        headers = {"Authorization": f"Bearer {bearer_token}"}

        async with httpx.AsyncClient(timeout=15) as client:
            for account in accounts:
                try:
                    user_url = f"https://api.twitter.com/2/users/by/username/{account}"
                    user_resp = await client.get(user_url, headers=headers)
                    user_resp.raise_for_status()
                    user_data = user_resp.json()
                    user_id = user_data.get("data", {}).get("id")
                    if not user_id:
                        continue

                    tweet_url = f"https://api.twitter.com/2/users/{user_id}/tweets"
                    params = {
                        "max_results": 5,
                        "tweet.fields": "created_at,public_metrics",
                    }
                    tweet_resp = await client.get(
                        tweet_url, headers=headers, params=params
                    )
                    tweet_resp.raise_for_status()
                    tweet_data = tweet_resp.json()

                    for tweet in tweet_data.get("data", []):
                        text = tweet.get("text", "")
                        tw_id = tweet.get("id", "")
                        created = tweet.get("created_at", "")

                        pub_dt = None
                        if created:
                            try:
                                pub_dt = datetime.fromisoformat(
                                    created.replace("Z", "+00:00")
                                )
                            except ValueError:
                                pass

                        score = self.keyword_score(text, keywords)
                        items.append(ContentItem(
                            title=text[:150],
                            url=f"https://x.com/{account}/status/{tw_id}",
                            summary=text[:300],
                            source="twitter",
                            source_name=f"@{account}",
                            published=pub_dt,
                            relevance_score=score,
                        ))
                except Exception as e:
                    logger.error(f"Twitter API @{account} 失败: {e}")

        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", text).strip()
