---
kind: external_dependency
name: OKX 加密货币行情与 WebSocket 行情源
slug: okx
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
---

### OKX 行情接入
- BTC 实时行情通过 CCXT 公共接口拉取，默认交易所为 OKX（`_CCXT_EXCHANGE_ID = "okx"`），REST 基础 URL 为 `https://www.okx.com`。
- 可选启用 OKX WebSocket 低延迟行情：`btc_volatility_monitor_use_websocket=true` 时注入 `OKXTickerWebSocketQuoteFetcher`，连接 `wss://ws.okx.com:8443/ws/v5/public`，订阅 `tickers` 频道 `BTC-USDT`。
- WebSocket 不可用或缓存过期时自动回退到 REST 获取，保证运行时契约不变。
- 交易对符号标准化为 `BTCUSDT`，市场符号格式为 `BTC/USDT`。
- 验证具体 API/参数以官方文档为准。