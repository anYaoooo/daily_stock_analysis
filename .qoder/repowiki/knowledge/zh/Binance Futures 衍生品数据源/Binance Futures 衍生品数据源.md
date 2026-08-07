---
kind: external_dependency
name: Binance Futures 衍生品数据源
slug: binance-futures
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### Binance Futures 数据接入
- 回测系统使用 Binance Futures 公共接口获取资金费率（funding rate）和持仓量（open interest）等衍生品数据。
- 永续合约模拟包含资金费率累计、标记价格强平估算（维持保证金率 0.5%）、maker/taker 手续费区分。
- 跨所数据质量字段用于评估数据可靠性，单源时需降置信度。
- 验证具体 API/参数以官方文档为准。