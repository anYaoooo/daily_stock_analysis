# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api.v1.endpoints.stocks import _build_stock_news_items
from api.v1.endpoints.stocks import _btc_news_zh_title_and_summary
from api.v1.endpoints.stocks import FutureTimeoutError
from src.services.cryptopanic_news_service import CryptoPanicNewsItem, CryptoPanicNewsService
from src.search_service import SearchResponse, SearchResult


def test_build_stock_news_items_maps_cryptopanic_results() -> None:
    news_response = SearchResponse(
        query="Bitcoin BTC latest news",
        provider="CryptoPanic",
        success=True,
        results=[
            SearchResult(
                title="Fed rate-cut odds lift Bitcoin liquidity",
                snippet="BTC moved as markets repriced rates and CPI expectations.",
                url="https://example.com/btc-fed-rates",
                source="CryptoPanic",
                published_date="2026-06-18",
                relevance_score=88,
                relevance_category="direct_company_news",
                relevance_reasons=["标题命中加密货币标识 Bitcoin"],
            )
        ],
    )
    search_service = SimpleNamespace(search_stock_news=MagicMock(return_value=news_response))

    with patch("src.search_service.get_search_service", return_value=search_service):
        items = _build_stock_news_items("BTC", "Bitcoin")

    assert len(items) == 1
    assert items[0].title == "Fed rate-cut odds lift Bitcoin liquidity"
    assert items[0].translated_title == "利率预期变化影响比特币流动性"
    assert items[0].summary_zh == "关注点：降息/利率预期重新定价；影响：可能改变资金对加密资产的配置意愿。"
    assert items[0].source == "CryptoPanic"
    search_service.search_stock_news.assert_called_once_with(
        stock_code="BTC",
        stock_name="Bitcoin",
        max_results=3,
    )


def test_build_stock_news_items_timeout_returns_empty_payload() -> None:
    class TimeoutFuture:
        def result(self, timeout=None):
            raise FutureTimeoutError()

    class TimeoutExecutor:
        def __init__(self, *args, **kwargs):
            self.shutdown_called = False

        def submit(self, fn):
            return TimeoutFuture()

        def shutdown(self, *, wait=True, cancel_futures=False):
            self.shutdown_called = True
            assert wait is False
            assert cancel_futures is True

    with patch("api.v1.endpoints.stocks.ThreadPoolExecutor", TimeoutExecutor):
        assert _build_stock_news_items("BTC", "Bitcoin") == []


def test_cryptopanic_news_reads_chroma_cache_only() -> None:
    cached_item = CryptoPanicNewsItem(
        title="Bitcoin BTC liquidity improves",
        source="cryptopanic.com",
        time_ago="12min",
        coins=("BTC",),
        fetch_time="2026-06-22T08:00:00",
    )
    service = CryptoPanicNewsService(refresh_interval_seconds=0)
    service.read_from_chroma = MagicMock(return_value=[cached_item])
    service.fetch_news = MagicMock(side_effect=AssertionError("fresh fetch should not run when cache is available"))

    result = service.get_latest_news(coin="BTC", limit=1)

    assert result.provider == "CryptoPanicChroma"
    assert result.cache_used is True
    assert result.items == [cached_item]
    service.fetch_news.assert_not_called()


def test_cryptopanic_news_uses_stale_chroma_without_fetching() -> None:
    cached_item = CryptoPanicNewsItem(
        title="Bitcoin BTC stale cache item",
        source="cryptopanic.com",
        time_ago="2d",
        coins=("BTC",),
        fetch_time="2026-06-20T08:00:00",
    )
    service = CryptoPanicNewsService(refresh_interval_seconds=0)
    service.read_from_chroma = MagicMock(side_effect=[[], [cached_item]])
    service.fetch_news = MagicMock(side_effect=AssertionError("OpenCLI fetch must not run"))

    result = service.get_latest_news(coin="BTC", limit=1)

    assert result.provider == "CryptoPanicChromaStale"
    assert result.cache_used is True
    assert result.items == [cached_item]
    assert "已使用过期缓存" in (result.error_message or "")
    service.read_from_chroma.assert_any_call(coin="BTC", limit=1, allow_stale=True)
    service.fetch_news.assert_not_called()


def test_btc_news_local_summary_handles_dollar_index_and_bond_market() -> None:
    title_zh, summary_zh = _btc_news_zh_title_and_summary(
        "Bitcoin's nemesis, the Dollar Index, is on the verge of a major breakout"
    )
    assert title_zh == "美元指数接近关键突破，比特币承压"
    assert "美元指数走强" in summary_zh

    title_zh, summary_zh = _btc_news_zh_title_and_summary(
        "The bond market is flashing a clear signal on interest rates. Bitcoin bulls should take note"
    )
    assert title_zh == "债券市场释放利率信号，比特币多头需关注"
    assert "美债收益率和利率预期变化" in summary_zh
