# -*- coding: utf-8 -*-
"""
===================================
数据源策略层 - 包初始化
===================================

BTC-only 模式下包入口仅导出 BTC 行情所需 provider，避免默认导入股票市场依赖。
"""

from .base import BaseFetcher, DataFetcherManager
from .crypto_fetcher import CryptoFetcher, is_crypto_code

__all__ = [
    'BaseFetcher',
    'DataFetcherManager',
    'CryptoFetcher',
    'is_crypto_code',
]
