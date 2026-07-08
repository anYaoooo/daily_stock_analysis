# BTC 正式交易回测系统说明

本文档说明当前 BTC 回测系统的算法、数据流、指标口径、接口、配置、能力边界和后续优化方向。当前实现定位为“基于分析报告交易计划的轻量正式交易回测”：它不是简单判断涨跌对错，而是把报告中的多空交易计划转换为可审计的模拟交易，并计算交易成本、仓位、资金曲线和风险收益指标。

## 1. 系统定位

当前 BTC 回测系统评估的是历史 BTC 分析报告中给出的交易计划，而不是独立寻找交易信号。它回答的问题是：

- 报告给出的多单、空单或日内计划是否在后续行情中触发入场。
- 入场后先触发止盈、止损，还是持有到评估窗口结束。
- 在考虑手续费、滑点、仓位和风险预算后，该计划对账户权益的贡献是多少。
- 多个历史计划累计后的胜率、收益、回撤和资金曲线如何。

因此，它更接近“AI 分析报告交易计划验收系统”，不是全市场参数寻优或高频撮合引擎。

## 2. 主要代码入口

| 模块 | 职责 |
| --- | --- |
| `src/services/crypto_backtest_service.py` | 回测编排：筛选历史报告、抽取交易计划、加载 K 线、调用引擎、保存结果和汇总 |
| `src/core/crypto_backtest_engine.py` | 核心算法：入场触发、止盈止损、交易成本、仓位、收益和风险指标计算 |
| `src/repositories/crypto_backtest_repo.py` | 数据访问：候选记录、结果保存、分页查询、汇总 upsert |
| `src/storage.py` | ORM 表：`crypto_backtest_results`、`crypto_backtest_summaries` |
| `api/v1/endpoints/backtest.py` | API：运行 BTC 回测、查询结果、查询表现 |
| `api/v1/schemas/backtest.py` | API 返回结构：结果项、汇总指标、资金曲线和交易明细 |

## 3. 数据来源

### 3.1 分析计划来源

回测从 `analysis_history.raw_result` 中提取 BTC 分析报告生成的交易计划。当前支持三类计划：

| plan_type | horizon | direction | 说明 |
| --- | --- | --- | --- |
| `daily_long` | `daily` | `long` | 日线多单计划 |
| `daily_short` | `daily` | `short` | 日线空单计划 |
| `intraday` | `intraday` | `long` / `short` / `wait` | 小时线日内计划 |

每个计划至少需要：

- `entry_price`：入场价。
- `stop_loss`：止损价。
- `take_profit`：止盈价。
- `direction`：多/空方向，日内计划会额外检查 enabled 状态。

计划结构还会透出 `entry_zone`、`invalid_condition`、`risk_reward`、`position_hint`、`confidence` 和 `no_trade_reason`，用于报告展示和人工复盘。回测引擎当前仍以可解析的 `entry_price`、`stop_loss`、`take_profit` 作为机器执行字段；缺失关键字段的计划会在历史记录入口标记为 `invalid_plan`，不会被静默当作有效交易。

每个计划回测结果还会在 `diagnostics.indicator_tags` 中保存生成报告时的低敏 BTC 指标快照标签，当前包括：

| 标签 | 来源 | 用途 |
| --- | --- | --- |
| `price_action.state` | 日线计划取日线上下文，日内计划优先取小时线上下文 | 区分突破、跌破、扫高扫低和区间 |
| `ema.structure` | 同上 | 识别 bullish / bearish / mixed 趋势结构 |
| `vwap.price_position` | 同上 | 判断价格位于 rolling VWAP 上方、下方或附近 |
| `volume.confirmation` | 同上 | 标记高量、低量或正常确认 |
| `volatility.atr14_pct` | 同上 | 记录当时 ATR14% 波动环境 |
| `intraday.alignment` | 多周期上下文 | 区分顺日线、逆日线短线和等待触发 |
| `event.type` | 日线或小时线事件上下文 | 标记急跌、扫低、反弹候选等事件 |
| `derivatives.funding_state` | Binance Futures 公共 funding rate 上下文 | 标记多空资金费率拥挤度 |
| `derivatives.open_interest_state` | Binance Futures 公共 open interest 上下文 | 标记持仓量/名义规模可用性和高持仓环境 |
| `derivatives.leverage_pressure` | funding + OI 派生摘要 | 标记多头拥挤、空头拥挤或中性杠杆压力 |

这些标签只保存结构化摘要，不保存 prompt 或新闻原文，用于后续按指标组合复盘和风控降权。

### 3.2 行情来源

服务通过 `CryptoFetcher.get_kline_data()` 获取 BTC K 线：

- 日线计划使用 daily K 线。
- 日内计划使用 hourly K 线。

回测窗口：

- `intraday`：从报告生成时刻开始，向后 24 小时。
- `daily_long` / `daily_short`：从报告生成日期的下一天 00:00 开始，向后 1 天。

注意：当前是用后验获取的 K 线执行回测，没有把当时可见的数据快照固化到结果中。

## 4. 核心回测算法

### 4.1 候选报告筛选

`CryptoBacktestRepository.get_candidates()` 会筛选：

1. `analysis_history.created_at <= now - CRYPTO_BACKTEST_MIN_AGE_HOURS`。
2. 非 `market_review` 报告。
3. code 是 BTC 或等价 BTC 代码。
4. 非 force 模式下，跳过同一 engine_version 已经回测过的分析记录。

### 4.2 入场触发

对每个计划，回测引擎在评估窗口内扫描 K 线：

```text
如果某根 K 线满足 low <= entry_price <= high
则认为该计划在这根 K 线触发入场
否则 outcome = no_entry
```

未触发入场的计划计入 completed，但不参与方向正确率和胜负统计。

### 4.3 出场规则

入场后，从触发入场的 K 线开始扫描后续 K 线。

多单：

- `low <= stop_loss` 视为触发止损。
- `high >= take_profit` 视为触发止盈。

空单：

- `high >= stop_loss` 视为触发止损。
- `low <= take_profit` 视为触发止盈。

若同一根 K 线同时触发止盈和止损，当前采用保守口径：

```text
first_hit = ambiguous
exit_reason = ambiguous_stop_loss
exit_price = stop_loss
```

若评估窗口内没有触发止盈或止损，则以窗口最后一根 K 线的 close 作为退出价。

### 4.4 交易成本

当前模型支持单边手续费和单边滑点：

```text
fee_rate = CRYPTO_BACKTEST_FEE_RATE_BPS / 10000
slippage_rate = CRYPTO_BACKTEST_SLIPPAGE_BPS / 10000
```

多单：

```text
executed_entry_price = entry_price * (1 + slippage_rate)
executed_exit_price = exit_price * (1 - slippage_rate)
gross_pnl = (executed_exit_price - executed_entry_price) * quantity
```

空单：

```text
executed_entry_price = entry_price * (1 - slippage_rate)
executed_exit_price = exit_price * (1 + slippage_rate)
gross_pnl = (executed_entry_price - executed_exit_price) * quantity
```

手续费：

```text
entry_fee = abs(executed_entry_price * quantity) * fee_rate
exit_fee = abs(executed_exit_price * quantity) * fee_rate
total_fee = entry_fee + exit_fee
net_pnl = gross_pnl - total_fee
r_multiple = net_pnl / risk_budget
```

### 4.5 仓位 sizing

默认按账户风险预算和止损距离推导仓位。

```text
initial_equity = CRYPTO_BACKTEST_INITIAL_EQUITY
risk_budget = initial_equity * CRYPTO_BACKTEST_RISK_PER_TRADE_PCT / 100
max_notional = initial_equity * CRYPTO_BACKTEST_MAX_NOTIONAL_PCT / 100 * CRYPTO_BACKTEST_LEVERAGE
```

若计划有有效止损：

```text
stop_distance = abs(entry_price - stop_loss)
risk_sized_qty = risk_budget / stop_distance
sizing_method = risk
```

若没有有效止损：

```text
risk_sized_qty = max_notional / entry_price
sizing_method = notional
```

最终仓位：

```text
max_qty = max_notional / entry_price
quantity = min(risk_sized_qty, max_qty)
position_notional = quantity * entry_price
```

这个口径能避免没有止损的计划无限放大仓位，同时让带止损的计划按真实交易风控思路计算。

### 4.6 单笔收益

单笔净收益率使用账户权益口径，不是价格涨跌幅：

```text
net_return_pct = net_pnl / initial_equity * 100
```

另外 diagnostics 中保留裸价格方向收益：

```text
gross_trade_return_pct = 多单: (exit_price - entry_price) / entry_price * 100
gross_trade_return_pct = 空单: (entry_price - exit_price) / entry_price * 100
```

### 4.7 胜负分类

当前使用净收益率做胜负分类：

```text
if net_return_pct >= CRYPTO_BACKTEST_NEUTRAL_BAND_PCT:
    outcome = win
elif net_return_pct <= -CRYPTO_BACKTEST_NEUTRAL_BAND_PCT:
    outcome = loss
else:
    outcome = neutral
```

方向正确率只统计已经触发入场且非中性的计划。

## 5. 指标体系

### 5.1 单笔结果字段

`/api/v1/backtest/crypto/results` 返回每条计划回测结果。

核心字段：

| 字段 | 含义 |
| --- | --- |
| `plan_type` | `daily_long` / `daily_short` / `intraday` |
| `horizon` | `daily` / `intraday` |
| `direction` | `long` / `short` / `wait` |
| `eval_status` | `completed` / `skipped` / `insufficient_data` |
| `entry_triggered` | 是否触发入场 |
| `first_hit` | `take_profit` / `stop_loss` / `ambiguous` / `neither` |
| `simulated_return_pct` | 单笔账户净收益率 |
| `trade` | 仓位、成交价、手续费、净 PnL 等交易明细 |
| `execution` | 本次回测使用的手续费、滑点、权益、杠杆和风控参数 |
| `diagnostics` | 完整诊断信息 |
| `diagnostics.indicator_tags` | 回测时固化的 BTC 指标快照标签 |

结果接口支持过滤参数：

| 参数 | 说明 |
| --- | --- |
| `horizon=daily|intraday` | 按日线主计划或小时线日内计划过滤 |
| `plan_type=daily_long|daily_short|intraday` | 按计划类型过滤 |
| `direction=long|short|wait` | 按计划方向过滤 |
| `result_status=win|loss|neutral|no_entry|skipped|insufficient_data` | 按回测结果状态过滤 |

`trade` 结构主要包含：

| 字段 | 含义 |
| --- | --- |
| `initial_equity` | 初始权益 |
| `risk_budget` | 单笔风险预算 |
| `position_notional` | 名义仓位 |
| `quantity` | 模拟交易数量 |
| `sizing_method` | `risk` 或 `notional` |
| `executed_entry_price` | 含滑点入场价 |
| `executed_exit_price` | 含滑点出场价 |
| `gross_pnl` | 扣费前盈亏 |
| `entry_fee` | 入场手续费 |
| `exit_fee` | 出场手续费 |
| `total_fee` | 总手续费 |
| `net_pnl` | 净盈亏 |
| `r_multiple` | 单笔 R 倍数，`net_pnl / risk_budget` |
| `net_return_pct` | 账户净收益率 |
| `gross_trade_return_pct` | 裸价格方向收益率 |

`diagnostics.data_snapshot` 会保存本次回测使用的 K 线来源、周期、拉取时间、bar 数量、数据范围和 `sha256` 数据哈希；`diagnostics.lookahead_guard` 会保存评估窗口与最早 forward bar 是否晚于报告生成时间，用于审计前视偏差。

### 5.2 汇总指标

`/api/v1/backtest/crypto/performance` 返回汇总表现。

基础统计：

| 指标 | 含义 |
| --- | --- |
| `total_evaluations` | 总计划数 |
| `completed_count` | 完成评估数 |
| `triggered_count` | 入场触发数 |
| `no_entry_count` | 未触发入场数 |
| `skipped_count` | 跳过数 |
| `insufficient_count` | 数据不足数 |
| `win_count` | 胜数 |
| `loss_count` | 负数 |
| `neutral_count` | 中性数 |
| `direction_accuracy_pct` | 方向正确率 |
| `win_rate_pct` | 胜率，胜 / (胜 + 负) |
| `avg_simulated_return_pct` | 平均单笔账户净收益率 |

交易风险指标：

| 指标 | 含义 |
| --- | --- |
| `risk_metrics.initial_equity` | 资金曲线初始权益 |
| `risk_metrics.final_equity` | 资金曲线最终权益 |
| `risk_metrics.total_return_pct` | 总账户收益率 |
| `risk_metrics.total_net_pnl` | 累计净盈亏 |
| `risk_metrics.total_fees` | 累计手续费 |
| `risk_metrics.profit_factor` | 总盈利 / 总亏损绝对值 |
| `risk_metrics.avg_trade_net_pnl` | 平均单笔净盈亏 |
| `risk_metrics.best_trade_return_pct` | 最佳单笔账户收益率 |
| `risk_metrics.worst_trade_return_pct` | 最差单笔账户收益率 |
| `risk_metrics.avg_r_multiple` | 平均 R 倍数 |
| `risk_metrics.best_r_multiple` | 最佳单笔 R 倍数 |
| `risk_metrics.worst_r_multiple` | 最差单笔 R 倍数 |
| `risk_metrics.max_drawdown_pct` | 资金曲线最大回撤 |
| `risk_metrics.expectancy_pct` | 单笔收益期望 |
| `equity_curve` | 按回测交易顺序生成的权益曲线 |

`diagnostics.sample_confidence` 会按 `triggered_count < 30` 标记低置信度样本，避免少量成交样本被误读为稳定策略结论。

`diagnostics.indicator_group_breakdown` 会按计划类型、方向、多周期对齐、价格行为、VWAP、EMA、量能确认和事件类型分组。每个分组 bucket 展示样本数、触发数、胜率、平均净收益、最大回撤、平均 R 倍数和低样本置信提示。

## 6. API 使用

### 6.1 运行 BTC 回测

```bash
curl -X POST http://127.0.0.1:8000/api/v1/backtest/crypto/run \
  -H "Content-Type: application/json" \
  -d '{"code":"BTC","force":false,"min_age_days":1,"limit":200}'
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `code` | 可选，指定 BTC 或等价代码 |
| `force` | 是否重算已有 engine_version 的结果 |
| `min_age_days` | 最小报告天龄，会换算为小时 |
| `limit` | 最多处理的历史报告数 |

### 6.2 查询历史分析记录回测入口

```bash
curl "http://127.0.0.1:8000/api/v1/backtest/crypto/history?code=BTC&page=1&limit=20"
```

返回内容以 `analysis_history_id` 为主对象，每条记录包含：

- 报告时间、分析周期、摘要和当前整条记录的 `backtest_status`。
- `plans[]`：每个计划的 `plan_type`、方向、点位、风险收益比、仓位建议、置信度、缺失字段、是否可回测。
- `latest_result`：该计划最近一次回测摘要，包含是否触发入场、收益率、交易明细和状态。

### 6.3 按历史记录回测

```bash
curl -X POST http://127.0.0.1:8000/api/v1/backtest/crypto/run-selected \
  -H "Content-Type: application/json" \
  -d '{"analysis_history_ids":[123,124],"plan_types":["daily_long","intraday"],"force":false}'
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `analysis_history_ids` | 要回测的历史分析记录主键 ID 列表 |
| `plan_types` | 可选，只回测指定计划类型 |
| `force` | 是否替换同一记录、同一计划、同一 engine_version 的已有结果 |

非 `force` 模式下，已存在的计划级结果会被跳过，避免产生难以解释的重复记录。`force=true` 只替换本次涉及的计划类型，不会删除同一报告其他计划的结果。

### 6.4 查询回测结果

```bash
curl "http://127.0.0.1:8000/api/v1/backtest/crypto/results?code=BTC&page=1&limit=20"
```

可选过滤：

- `horizon=daily|intraday`
- `plan_type=daily_long|daily_short|intraday`

### 6.5 查询汇总表现

```bash
curl "http://127.0.0.1:8000/api/v1/backtest/crypto/performance?scope=overall"
```

可选 scope：

| scope | 说明 |
| --- | --- |
| `overall` | 全部 BTC 回测 |
| `code` | 按代码 |
| `horizon` | 按 daily / intraday |
| `plan_type` | 按计划类型 |

示例：

```bash
curl "http://127.0.0.1:8000/api/v1/backtest/crypto/performance?scope=plan_type&plan_type=daily_long"
```

### 6.4 删除单条 BTC 回测结果

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/backtest/crypto/results/123?plan_type=daily_long"
```

删除按当前 `CRYPTO_BACKTEST_ENGINE_VERSION`、`analysis_history_id` 和 `plan_type` 定位单条结果。删除后服务会重算 BTC 回测汇总；若没有匹配记录，返回 `{"deleted":0}`。

## 7. 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CRYPTO_BACKTEST_MIN_AGE_HOURS` | `24` | 报告生成后至少等待多少小时再评估 |
| `CRYPTO_BACKTEST_ENGINE_VERSION` | `btc-plan-v2` | 回测引擎版本，算法升级后用于隔离旧结果 |
| `CRYPTO_BACKTEST_NEUTRAL_BAND_PCT` | `0.2` | 中性区间阈值，账户净收益率落在该区间内视为 neutral |
| `CRYPTO_BACKTEST_INITIAL_EQUITY` | `10000` | 初始权益 |
| `CRYPTO_BACKTEST_RISK_PER_TRADE_PCT` | `1.0` | 单笔风险占账户权益百分比 |
| `CRYPTO_BACKTEST_MAX_NOTIONAL_PCT` | `100.0` | 单笔最大名义仓位占账户权益百分比 |
| `CRYPTO_BACKTEST_LEVERAGE` | `1.0` | 名义杠杆倍数 |
| `CRYPTO_BACKTEST_FEE_RATE_BPS` | `5.0` | 单边手续费，单位 bps |
| `CRYPTO_BACKTEST_SLIPPAGE_BPS` | `2.0` | 单边滑点，单位 bps |

## 8. 当前评估

### 8.1 已经具备的正式交易回测能力

当前系统已经具备以下正式回测特征：

- 明确的交易计划输入：方向、入场、止损、止盈。
- 明确的未来评估窗口，避免用报告生成前的行情验证计划。
- 支持多单和空单。
- 支持入场未触发、跳过、数据不足等状态区分。
- 支持手续费和滑点。
- 支持按账户风险预算和最大名义仓位推导仓位。
- 单笔结果保留交易明细，可审计。
- 单笔结果保留 R 倍数、K 线数据快照哈希和前视偏差校验信息。
- 汇总层对入场触发样本小于 30 的结果标记低置信度。
- 汇总层提供资金曲线、总收益、最大回撤、profit factor、期望值等指标。
- API 和数据库结果按 engine_version 隔离，便于算法升级后重算。

### 8.2 主要不足

当前系统仍有以下限制：

1. K 线内路径不可知。
   使用 OHLC K 线时，无法知道同一根 K 线内价格先到止盈还是先到止损。当前采用“同时触发时按止损退出”的保守口径，但这不等同于真实撮合。

2. 资金曲线不是严格复利。
   单笔 sizing 使用固定 initial_equity，而不是每笔交易后的动态权益。因此资金曲线用于评估策略结果，但不是完全真实的逐笔复利账户。

3. 未处理并发持仓。
   如果多个计划时间重叠，当前汇总按顺序累加 PnL，没有检查同一账户是否同时占用保证金、是否超过组合风险上限。

4. 没有真实订单簿与成交量约束。
   当前用固定滑点模拟成交，没有考虑盘口深度、成交量、资金费率、强平价格、maker/taker 手续费差异。

5. 数据快照未完整固化。
   回测结果已保存 K 线元数据和哈希，便于发现历史数据修正；但尚未保存完整 bar payload，因此不能离线重放当时的全部行情。

6. 置信区间未显式展示。
   已有低样本标记、总样本、胜率和收益指标，但没有 Wilson 区间、bootstrap 置信区间、按市场环境分层的统计显著性。

7. 计划质量未参与评分。
   当前只评估计划执行后结果，没有把计划结构质量、盈亏比、入场距离、止损合理性等作为单独指标。

### 8.3 综合评级

按交易回测成熟度分层：

| 层级 | 描述 | 当前状态 |
| --- | --- | --- |
| L0 | 只判断涨跌对错 | 已超过 |
| L1 | 验证交易计划是否触发、是否止盈止损 | 已具备 |
| L2 | 加入手续费、滑点、仓位、净 PnL 和资金曲线 | 已具备 |
| L3 | 支持复利权益、组合持仓、保证金、资金费率、数据快照 | 部分缺失 |
| L4 | tick/订单簿级撮合、参数寻优、walk-forward、蒙特卡洛 | 未具备 |

当前系统可评为 L2，适合评估 BTC 分析报告的历史交易质量；若要作为真实资金部署前的策略验证系统，还需要补齐 L3。

## 9. 建议优先级

### P0：让结果更可信

- 已落地：默认引擎版本升级为 `btc-plan-v2`，每次回测保存 K 线来源、周期、拉取时间、bar 数量、数据范围和哈希。
- 已落地：增加前视偏差校验、低样本置信标记和单笔 R 倍数。
- 后续可继续扩展完整 K 线 payload 快照，支持离线复算和外部审计。

```text
r_multiple = net_pnl / risk_budget
```

### P1：更接近真实账户

- 使用动态权益 sizing：每笔交易按上一笔后的 equity 计算 risk_budget。
- 加入组合风险限制：同时未平仓计划的总风险不能超过账户上限。
- 增加资金费率、保证金占用和强平风险估算。
- 对 maker/taker 手续费分开配置。

### P2：更强的研究能力

- 增加 walk-forward 分段评估。
- 增加按行情状态分层统计，例如趋势、震荡、高波动、低波动。
- 增加 bootstrap 置信区间。
- 增加参数敏感性分析，例如不同手续费、滑点、风险比例下的结果。

### P3：更完整的撮合能力

- 引入更细周期数据，例如 1m K 线，用于降低同根 K 线止盈止损顺序不确定性。
- 若接入交易所历史成交或订单簿，可扩展为事件驱动撮合。
- 保存完整 trade ledger，而不是只放在 diagnostics JSON。

## 10. 使用建议

解读结果时建议优先看：

1. `triggered_count`：入场触发样本是否足够。
2. `win_rate_pct`：胜率，但不要单独使用。
3. `risk_metrics.total_return_pct`：账户口径总收益。
4. `risk_metrics.max_drawdown_pct`：最大回撤。
5. `risk_metrics.profit_factor`：盈亏结构。
6. `risk_metrics.expectancy_pct`：单笔期望。
7. `plan_type_breakdown`：多单、空单、日内计划谁贡献更好。

如果出现以下情况，不应认为策略已经有效：

- 样本数很少。
- 胜率高但 profit factor 低。
- 总收益为正但最大回撤过高。
- 多数计划没有触发入场。
- 收益主要来自极少数单笔交易。

## 11. 结论

当前 BTC 回测系统已经具备正式交易回测的基础骨架：它能把 AI 报告中的交易计划转成可审计交易，纳入手续费、滑点、仓位、风险预算和资金曲线，并通过 API 返回单笔与汇总指标。

但它仍应被视为“报告计划级交易回测”，不是完整机构级撮合系统。最关键的后续提升是：动态权益、组合风险、数据快照、样本置信度和更细粒度行情。补齐这些以后，它才更适合承担真实交易系统上线前的策略验收职责。

## 12. 建议
[Phase 1: P0 可信度防线] ──────► [Phase 2: P1 真实账户环境] ──────► [Phase 3: P2-P3 深度研究与精细撮合]
1. 升级引擎至 v2                 1. 状态机动态复利重构               1. 1m K线局部降维路由
2. 增加前视偏差防作弊校验          2. 并发持仓管理器与风控熔断          2. 静态/动态计划质量评分
3. K线数据哈希与元数据固化         3. 永续合约资金费率与多空费用拆分     3. 统计显著性（Bootstrap置信区间）
