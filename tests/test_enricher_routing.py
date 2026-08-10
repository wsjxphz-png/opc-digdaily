"""
回归测试：enricher 全文提取的源路由（修复国内 0/172 的死板白名单）。

曾经 enricher 只用白名单 ("rss","weixin","weixin_whitelist") 决定抓不抓正文，
导致国内主力来源 chinese-search / weixin_targets 的文章从不被抓 → AI 只见标题摘要
→ 国内 172 条候选 0 条进 AI → 国内 0 推送。现改为黑名单（视频/音频类跳过，其余抓）。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.base import ContentItem
from sources.enricher import ContentEnricher, NO_FETCH_FULLTEXT


def _item(source: str) -> ContentItem:
    return ContentItem(
        title=f"测试标题-{source}",
        url="https://example.com/article",
        summary="摘要",
        source=source,
        source_name="测试",
    )


def _fake_response():
    r = MagicMock()
    r.status_code = 200
    r.text = "<html><body><article><p>这是一段足够长的正文内容，用于验证 trafilatura 能提取出来，长度必须超过一百个字符才能被认定为成功的全文提取结果。</p></article></body></html>"
    return r


async def run_enrich(items):
    # 拦截真实网络与真实 trafilatura，用可控桩验证「路由」是否正确
    with patch("sources.enricher.httpx.AsyncClient") as MockClient, \
         patch("sources.enricher.trafilatura.extract", return_value="提取到的正文" * 20):
        inst = MockClient.return_value.__aenter__.return_value
        inst.get = MagicMock(return_value=asyncio.Future())
        inst.get.return_value.set_result(_fake_response())
        enricher = ContentEnricher(concurrency=5, timeout=5)
        return await enricher.enrich(items)


def test_chinese_search_and_weixin_targets_now_enriched():
    """曾经被白名单跳过的两个国内主力源，现在应该抓到正文。"""
    items = [_item("chinese-search"), _item("weixin_targets"), _item("rss"), _item("weixin")]
    out = asyncio.run(run_enrich(items))
    for it in out:
        assert it.full_text, f"{it.source} 应被抓正文，但 full_text 为空"
    print("✓ chinese-search / weixin_targets / rss / weixin 均抓到正文")


def test_video_audio_sources_skipped():
    """视频/音频类（youtube/twitter/bilibili/xiaoyuzhou）跳过，不抓正文。"""
    items = [_item(s) for s in NO_FETCH_FULLTEXT]
    out = asyncio.run(run_enrich(items))
    for it in out:
        assert not it.full_text, f"{it.source} 是视频/音频，不应抓正文"
    print(f"✓ 黑名单源 {sorted(NO_FETCH_FULLTEXT)} 均跳过")


def test_reddit_not_treated_as_article():
    """reddit 走独立评论分支，不会被当文章抓（无全文，但不崩）。"""
    items = [_item("reddit")]
    out = asyncio.run(run_enrich(items))
    assert not out[0].full_text
    print("✓ reddit 走独立分支，未当文章抓")


def test_keyword_score_is_source_method():
    """回归：keyword_score 必须是 BaseSource 的方法，子类 self.keyword_score 可调用。

    曾误改成模块级函数，导致 RSSSource/RedditSource/TwitterSource/YouTubeSource
    采集时 AttributeError、所有 RSS 源全线挂掉却没被单测发现。
    """
    from sources.rss import RSSSource
    s = RSSSource()
    assert hasattr(s, "keyword_score"), "RSSSource 必须有 keyword_score 方法"
    kw = {"strong": ["副业", "月入"], "weak": ["小红书"]}
    assert s.keyword_score("宝妈做手工皂副业月入3000", kw) > 0
    assert s.keyword_score("今日天气晴", kw) == 0.0
    print("✓ keyword_score 是 BaseSource 方法，子类可正常调用")


if __name__ == "__main__":
    test_chinese_search_and_weixin_targets_now_enriched()
    test_video_audio_sources_skipped()
    test_reddit_not_treated_as_article()
    test_keyword_score_is_source_method()
    print("\n全部通过")
