# BTC 正式交易回测系统说明

本文档说明当前 BTC 回测系统的算法、数据流、指标口径、接口、配置、能力边界和后续优化方向。当前实现定位为“基于分析报告交易计划的轻量正式交易回测”：它不是简单判断涨跌对错，而是把报告中的多空交易计划转换为可审计的模拟交易，并计算交易成本、仓位、资金曲线和风险收益指标。

## 1. 系统定位

当前 BTC 回测系统评估的是历史 BTC 分析报告中给出的交易计划，而不是独立寻找交易信号。它回答的问题是：

- 报告给出的多单、空单或日内计划是否在后续行情中形成交易信号。
- 信号形成后，下一根 K 线的实际成交价是否仍满足风控，委托最终是成交还是被拒绝。
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
| `src/services/crypto_market_data_service.py` | 本地优先的闭合 K 线访问：SQLite 缓存完整覆盖时直接读取；范围缺失时通过现有 `CryptoFetcher` 同步并保存快照元数据。 |
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

`btc-plan-v5` 的每个可回测计划至少需要：

- `entry_price`：入场价。
- `stop_loss`：止损价。
- `take_profit`：止盈价。
- `direction`：多/空方向，日内计划会额外检查 enabled 状态。
- `execution_contract`：版本为 `btc-execution-v1` 的机器执行契约，完整表达执行模板、价格触达、收盘、量比、VWAP、确认 bars、等待 bars、成交方式和最长持有 bars。

计划结构还会透出 `entry_zone`、`invalid_condition`、`risk_reward`、`position_hint`、`confidence` 和 `no_trade_reason`，用于报告展示和人工复盘。新报告额外提供 `execution_ladder`，按试仓（`trial_entry`）、确认加仓（`confirmation_add`）和失效退出（`invalidation`）分步展示。顶层 `entry_price`、`entry_zone`、`trigger_condition` 和 `execution_contract` 始终对应试仓层，因此回测不把确认加仓当成第二笔模拟成交，也不会从自然语言猜测加仓条件；`stop_loss` 与 `invalidation.price` 使用同一失效位。`execution_contract.entry.setup_type` 支持 `breakout` 和 `pullback`：突破计划要求量能确认，回踩计划可用固定价格触达加收盘收复/承压表达，不强制要求量比门槛。BTC 分析完成后会复用 v5 的静态执行校验：不满足风险收益、成本覆盖、量能或执行契约要求的方案会保留原计划，但标注为 `validation_status=failed`，仅供人工复核，不会被误写成已通过校验的策略。日内计划额外检查闭合日线与小时线的方向：两周期已同向、且均与计划方向相反时，计划会降为 `direction=wait` 并写明等待原因；小时线本身支持的逆日线短线机会仍允许保留。多单同时出现 `bearish_push` 与 VWAP 下方，或低量且没有收盘突破确认时，也会由确定性可交易门直接改为等待；逆日线且缺少小时线确认的日内计划同样阻断。仍保留的逆日线短线计划会写入 `tradeability_status=countertrend_limited`，将仓位乘数上限限制为 50%，并把入场等待窗口收紧到最多 6 根小时线；限仓和有效期会透传到回测使用的执行契约。衍生品、订单流、EWMA 或宏观层缺失时不再解释为中性，而是写入 `tradeability_status=degraded_missing_context`，并把仓位乘数上限收紧至 50%；已有更严格上限时继续保留更严格值。EWMA 风险覆盖生成的 `position_multiplier_cap` 会被回测仓位计算实际消费，同时作用于风险预算、最大名义仓位和成交数量；若计划已有更严格的仓位上限，系统只保留两者中的较小值，不会因重复对齐或普通波动状态放大仓位。报告自然语言与回测必须引用同一份 `execution_contract`；缺失契约、契约不完整或包含不支持条件的交易计划会标记为 `invalid_plan/skipped`，不会从文本猜测条件，也不会降级成触价成交。明确 `direction=wait` 的观望计划标记为 `no_trade_plan/skipped`，不把方向或执行契约误报为缺失，也不计入有效样本。v4 引入的标记价格、资金费率、强平与 maker/taker 成本在 v5 中继续保留；v5 额外增加计划价和实际成交价两阶段质量门槛，旧 v2/v3/v4 结果保留审计但不与 v5 指标混合。

同一 BTC、同一周期、同一方向已有活动 DecisionSignal 时，新分析产生的同方向候选计划会归档，并记录冻结它的活动信号；只有原计划过期、失效或方向反转时才允许新计划接管。观望信号不会阻挡新的可交易计划。这个生命周期约束用于避免趋势行情中每次分析都抬高入场价，导致计划持续追价而永远无法触发。

急跌反弹使用独立的 `entry.signal_class=selloff_rebound_trial` 执行路径。事件形成时，事件低点所在 K 线高点被冻结为第一层试仓确认价，不会因为后续收复 EMA20 或 VWAP 而继续上移。短周期波动监控已经形成 `entry_signal` 时，可在禁止追价线以内直接执行最多 25% 试仓；否则只需一根闭合小时线收于冻结确认价上方。量比只影响仓位与置信度，不再作为第一层试仓的硬门槛。第二层加仓需要一根闭合小时线确认，第三层才要求 EMA20/VWAP 回踩承接。当前价超过冻结确认价 0.5×ATR，或按当前价计算的剩余风险收益比低于 1:1.5 时，计划明确标记为机会错过。后续小时基线报告只在四小时内继承仍有效的同方向监控信号，并沿用原确认价和失效价。

当报告已经给出 `selloff_rebound_trial_ready` 试仓信号后，波动监控继续跟踪同一冻结确认价和失效价。首次跌回确认价下方只推送“疑似假跌破”并停止加仓；至少连续两次采样仍未收回时升级为“短周期有效跌破”，提示退出或减掉试仓。首次跌破后迅速收回才明确标记为“假跌破已收回”；若已经确认有效跌破后再站回，只提示“结构重新收回”，不会追溯改判成假跌破，也不会自动恢复原试仓。触及冻结失效价则不等待连续采样，立即推送信号失效并结束该计划。这里跟踪的是系统给出的试仓信号，不代表系统已经自动成交；实际仓位仍由用户确认。

每个计划回测结果还会在 `diagnostics.indicator_tags` 中保存生成报告时的低敏 BTC 指标快照标签，当前包括：

| 标签 | 来源 | 用途 |
| --- | --- | --- |
| `price_action.state` | 日线计划取日线上下文，日内计划优先取小时线上下文 | 区分突破、跌破、扫高扫低和区间 |
| `ema.structure` | 同上 | 识别 bullish / bearish / mixed 趋势结构 |
| `vwap.price_position` | 同上 | 判断价格位于 rolling VWAP 上方、下方或附近 |
| `volume.confirmation` | 同上 | 标记高量、低量或正常确认 |
| `volatility.atr14_pct` | 同上 | 记录当时 ATR14% 波动环境 |
| `volatility_forecast.regime` | 同周期闭合收益率的 EWMA 方差预测 | 标记 compressed / normal / elevated / extreme 波动状态 |
| `volatility_forecast.position_multiplier_cap` | EWMA 风险覆盖层 | 记录报告生成时的确定性仓位乘数上限 |
| `intraday.alignment` | 多周期上下文 | 区分顺日线、逆日线短线和等待触发 |
| `event.type` | 日线或小时线事件上下文 | 标记急跌、扫低、反弹候选等事件 |
| `derivatives.funding_state` | Binance Futures 公共 funding rate 上下文 | 标记多空资金费率拥挤度 |
| `derivatives.open_interest_state` | Binance Futures 公共 open interest 上下文 | 标记持仓量/名义规模可用性和高持仓环境 |
| `derivatives.leverage_pressure` | funding + OI 派生摘要 | 标记多头拥挤、空头拥挤或中性杠杆压力 |
| `order_flow.state` | Binance Futures 最近 5 分钟 K 线的主动买卖量聚合 | 标记 buy_dominant / sell_dominant / balanced |
| `order_flow.divergence` | 同窗口价格变化与 CVD | 标记价格/CVD 背离或量价同向状态；当前只作影子验证 |

这些标签只保存结构化摘要，不保存 prompt 或新闻原文，用于后续按指标组合复盘和风控降权。

### 3.2 行情来源

服务通过 `CryptoFetcher.get_kline_data()` 获取 BTC K 线：

- 日线计划使用 daily K 线。
- 日内计划使用 hourly K 线。

回测窗口由执行契约决定：

- `intraday`：从报告生成时刻开始，覆盖确认、下一根开盘成交、最长等待和最长持有需要的小时 bars。
- `daily_long` / `daily_short`：从报告生成日期的下一根完整日线开始，覆盖同样的日线 bars。

行情适配会剔除尚未闭合的小时线和日线。K 线来源、拉取时间、范围和哈希会写入 diagnostics；当前仍是后验拉取历史 K 线，不是交易所逐笔撮合回放。

### 3.3 OKX 历史训练数据回填

机器学习研究使用独立的分块回填入口，将 OKX `BTC-USDT-SWAP` 成交价和标记价 K 线保存到同一 SQLite 行情表：

```bash
python scripts/backfill_btc_history.py
python scripts/backfill_btc_history.py --export-csv
python scripts/backfill_btc_history.py --start 2021-01-01 --end 2024-12-31T23:00:00Z --chunk-days 30
```

默认口径：

- 周期为 1 小时，范围从 `2020-02-01 00:00 UTC` 到最新已闭合小时；这是 OKX 永续成交价与标记价可稳定对齐的起点，不代表 BTC 现货最早历史。
- 每 30 天为一个事务分块。SQLite 已完整覆盖的分块自动跳过，失败后可直接重跑；`--force` 才会强制覆盖。
- 每个分块要求成交价与标记价时间戳完整一致，并在结束时检查理论 bar 数、实际 bar 数、缺口以及执行价/标记价空值。
- `--export-csv [PATH]` 导出模型训练 CSV；省略 PATH 时输出到 `data/btc_okx_perpetual_1h_training.csv`。`data/` 已被 Git 忽略，训练数据不会进入仓库。

OKX 公共接口不能可靠提供覆盖完整永续历史的资金费率。训练回填不会补造旧 funding，而是保存空数组并令 `funding_complete=false`。这些 OHLCV/标记价数据可以用于收益率、波动率和方向概率模型，但 `btc-plan-v5` 严格永续回测仍要求窗口内每根 bar 的标记价与资金费率完整，因此不会误用这批缺失 funding 的旧历史。

## 4. 核心回测算法

### 4.1 候选报告筛选

`CryptoBacktestRepository.get_candidates()` 会筛选：

1. `analysis_history.created_at <= now - CRYPTO_BACKTEST_MIN_AGE_HOURS`。
2. 非 `market_review` 报告。
3. code 是 BTC 或等价 BTC 代码。
4. 非 force 模式下，跳过同一 engine_version 已经回测过的分析记录。

### 4.2 入场触发

对每个计划，回测引擎只在闭合 K 线上执行 `execution_contract.entry.conditions`。当前支持：

- `close_above` / `close_below`。
- `low_lte` / `high_gte`。
- `volume_ratio_gte` / `volume_ratio_lte`。
- `close_above_vwap` / `close_below_vwap`。

条件逻辑固定为 `all`。普通突破模板通常用收盘越过关键位加量比确认；急跌反弹试仓只使用冻结价的 `close_above` 且确认一根 K 线；回踩模板可用多单 `low_lte + close_above` 或空单 `high_gte + close_below`，表达价格触及固定回踩位后重新收复或承压。全部条件连续满足 `confirmation_bars` 后形成信号，计划尝试在下一根 K 线开盘成交；触发 K 线不参与持仓止盈止损。超过 `max_wait_bars` 仍未确认时才记为 `no_entry`。评估窗口尚未结束时记为 `insufficient_data/provisional`，不参与胜率。

信号形成不等于已经成交。v5 会用下一根开盘价重新检查止损/止盈几何、最低风险收益比和成本覆盖。若跳空后计划失真，则保留 `signal_triggered=true`，同时写入 `order_status=rejected`、拒单原因，以及拒单后评估窗口内的最大有利/不利波动；只有真实成交才写入 `entry_triggered=true`。这样可以区分“分析没有给出机会”和“机会出现但执行门槛过严或成交价恶化”。

### 4.3 出场规则

入场后，从实际成交 K 线开始扫描后续 K 线。

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

若 `exit.max_holding_bars` 内没有触发止盈或止损，则以最后一根完整 K 线的 close 作为退出价。窗口尚未成熟时保持暂定状态，不提前按当前 close 结算。

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
equity_before = previous_completed_equity if CRYPTO_BACKTEST_DYNAMIC_EQUITY_SIZING=true else CRYPTO_BACKTEST_INITIAL_EQUITY
risk_budget = equity_before * CRYPTO_BACKTEST_RISK_PER_TRADE_PCT / 100
max_notional = equity_before * CRYPTO_BACKTEST_MAX_NOTIONAL_PCT / 100 * CRYPTO_BACKTEST_LEVERAGE
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

`direction_accuracy_pct` 只统计独立成交且非中性的计划，代表含成本的成交后净结果命中率；`direction_accuracy_raw_pct` 使用信号触发后的纯价格 MFE/MAE 判断原始方向，不受手续费、滑点和资金费影响。两者分母必须在汇总诊断的 `metric_denominators` 中分别解释。

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
| `signal_triggered` | 交易条件是否已经成立 |
| `order_status` | `filled` / `rejected` / `pending_fill` / `not_triggered` 等委托状态 |
| `order_rejection_reason` | 信号成立但下一根实际成交价未通过风控时的拒单原因 |
| `entry_triggered` | 是否已经实际模拟成交 |
| `mfe_pct` / `mae_pct` | 信号触发后纯价格最大有利 / 不利偏移（不含成本） |
| `direction_correct_raw` | `mfe_pct > mae_pct` 时的原始方向命中标记 |
| `missed_favorable_move_pct` | 拒单后评估窗口内相对拒绝价的最大有利波动 |
| `missed_adverse_move_pct` | 拒单后评估窗口内相对拒绝价的最大不利波动 |
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
| `equity_before` | 本笔交易入场前可用权益；动态 sizing 开启时用于风险预算和净收益分母 |
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
| `total_evaluations` | 回测尝试总数，包含不可评估和仍在等待数据的计划，不等同于有效样本数 |
| `completed_count` | 完成评估数 |
| `triggered_count` | 去除重叠持仓后的实际成交数 |
| `no_entry_count` | 未实际成交数，可能包含未形成信号和信号后拒单 |
| `skipped_count` | 跳过数 |
| `insufficient_count` | 数据不足数 |
| `win_count` | 胜数 |
| `loss_count` | 负数 |
| `neutral_count` | 中性数 |
| `direction_accuracy_pct` | 成交后非中性净结果命中率（仅独立成交，已含成本） |
| `direction_accuracy_raw_pct` | 原始方向命中率（信号触发后，以纯价格 MFE/MAE 判断，不含成本） |
| `signal_quality_rate_pct` | 信号质量率：已形成信号 / 已完成评估 |
| `execution_fill_rate_pct` | 执行成交率：实际成交 / 已形成信号 |
| `win_rate_pct` | 账户层胜率：独立成交的胜 / (胜 + 负)，重叠持仓先排除 |
| `avg_simulated_return_pct` | 平均单笔账户净收益率 |
| `diagnostics.plan_level_result.win_rate_pct` | 计划层胜率：所有已成交计划的胜 / (胜 + 负)，包含重叠计划；用于评价计划本身，不代表账户可实现收益 |
| `diagnostics.signal_triggered_count` | 已形成交易信号的计划数 |
| `diagnostics.rejected_order_count` | 信号形成后因成交价质量门槛被拒绝的委托数 |
| `diagnostics.order_fill_rate_pct` | 实际成交数 / 信号形成数 |
| `diagnostics.avg_missed_favorable_move_pct` | 拒单后最大有利波动的样本均值 |
| `diagnostics.plan_quality_summary` | 计划质量四维分数（方向、位置、风险收益、执行）的样本均值 |
| `diagnostics.cost_sensitivity` | 手续费 5/10/15 bps 与滑点 2/5/10 bps 的 3×3 成本敏感性矩阵 |
| `diagnostics.risk_controls` | 组合风险、连续亏损和单日亏损护栏的审计结果；`would_block_new_trade=true` 时建议暂停新增交易 |
| `diagnostics.guard_forward_comparison` | 护栏前向队列：放行计划的实际结果、拦截计划的影子反事实结果、判定/原因计数及旧计划未分类数；影子结果不进入主指标 |

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

`diagnostics.sample_confidence` 会按独立 `triggered_count < 100` 标记低置信度样本，避免少量或高度重叠的成交样本被误读为稳定策略结论。`raw_triggered_count` 保留原始触发数，`overlap_excluded_count` 记录同一 BTC 持仓期间被排除的重叠信号。

Web 汇总使用 `completed_count` 表示已完成评估数，使用 `triggered_count` 表示独立成交样本数。缺少 `btc-execution-v1` 契约的旧报告会标记为不可评估，开放中的评估窗口会标记为等待数据；二者都不会显示成有效样本。

`diagnostics.indicator_group_breakdown` 会按计划类型、方向、多周期对齐、价格行为、VWAP、EMA、量能确认、EWMA 波动状态、主动买卖状态、价格/CVD 背离和事件类型分组。每个分组 bucket 展示已完成评估数、触发数、胜率、平均净收益、最大回撤、平均 R 倍数和低样本置信提示。订单流因子在样本充分前不会直接成为执行契约条件。

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

Web 回测页按该接口的分页结果展示历史分析表格。表格支持行级选择、当前页全选、批量回测和批量删除；点击记录或“详情”可打开抽屉查看该报告的全部计划、指标标签和单计划回测操作。

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

Web 的单条和批量回测使用异步接口，先提交再查询状态，避免长时间的数据拉取和计算占用浏览器请求：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/backtest/crypto/run-selected-async \
  -H "Content-Type: application/json" \
  -d '{"analysis_history_ids":[123,124],"plan_types":["daily_long"],"force":false}'

curl "http://127.0.0.1:8000/api/v1/backtest/crypto/tasks/<task_id>"
```

提交接口返回 HTTP `202` 和 `task_id`；状态接口在完成后返回与同步接口相同的回测统计。任务状态复用服务进程内队列，服务重启会终止未完成任务且原 `task_id` 不再可查询。同步 `run-selected` 接口保留给现有脚本和兼容调用。

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
| `CRYPTO_BACKTEST_ENGINE_VERSION` | `btc-plan-v5` | 计划质量门槛与永续合约执行引擎版本；与 v2/v3/v4 历史结果隔离 |
| `CRYPTO_BACKTEST_NEUTRAL_BAND_PCT` | `0.2` | 中性区间阈值，账户净收益率落在该区间内视为 neutral |
| `CRYPTO_BACKTEST_INITIAL_EQUITY` | `10000` | 初始权益 |
| `CRYPTO_BACKTEST_RISK_PER_TRADE_PCT` | `1.0` | 单笔风险占账户权益百分比 |
| `CRYPTO_BACKTEST_MAX_NOTIONAL_PCT` | `100.0` | 单笔最大名义仓位占账户权益百分比 |
| `CRYPTO_BACKTEST_LEVERAGE` | `1.0` | 名义杠杆倍数 |
| `CRYPTO_BACKTEST_FEE_RATE_BPS` | `5.0` | 单边手续费，单位 bps |
| `CRYPTO_BACKTEST_SLIPPAGE_BPS` | `2.0` | 单边滑点，单位 bps |
| `CRYPTO_BACKTEST_MAKER_FEE_RATE_BPS` | `2.0` | v4/v5 限价单单边手续费，单位 bps |
| `CRYPTO_BACKTEST_TAKER_FEE_RATE_BPS` | `5.0` | v4/v5 市价单单边手续费，单位 bps |
| `CRYPTO_BACKTEST_MAINTENANCE_MARGIN_RATE` | `0.005` | v4/v5 永续合约强平估算使用的维持保证金率 |
| `CRYPTO_BACKTEST_MINIMUM_RISK_REWARD` | `1.2` | v5 在计划价和实际成交价阶段要求的最低风险收益比 |
| `CRYPTO_BACKTEST_MINIMUM_VOLUME_RATIO` | `1.0` | v5 可交易计划中 `volume_ratio_gte` 的最低阈值 |
| `CRYPTO_BACKTEST_DYNAMIC_EQUITY_SIZING` | `true` | 是否按上一笔完成后的权益计算下一笔风险预算；关闭后使用固定初始权益 |
| `CRYPTO_BACKTEST_MAX_PORTFOLIO_RISK_PCT` | `2.0` | 组合风险审计阈值（仅诊断） |
| `CRYPTO_BACKTEST_CONSECUTIVE_LOSS_COOLDOWN` | `3` | 连续亏损后触发冷却的审计阈值（仅诊断） |
| `CRYPTO_BACKTEST_DAILY_LOSS_LIMIT_PCT` | `3.0` | 单日亏损保护审计阈值（仅诊断） |

## 8. 当前评估

### 8.1 已经具备的正式交易回测能力

当前系统已经具备以下正式回测特征：

- 明确的交易计划输入：方向、入场、止损、止盈。
- 明确的未来评估窗口，避免用报告生成前的行情验证计划。
- 支持多单和空单。
- 支持入场未触发、跳过、数据不足等状态区分。
- 支持手续费和滑点。
- v4 永续合约支持标记价格、资金费率、maker/taker 手续费和隔离保证金强平估算。
- v5 在生成计划价和下一根实际开盘价两次校验点位方向、风险收益比、成本覆盖和量能确认；跳空越过目标或使计划质量跌破门槛时记录为未成交。
- 支持按账户风险预算和最大名义仓位推导仓位。
- 单笔结果保留交易明细，可审计。
- 单笔结果保留 R 倍数、K 线数据快照哈希和前视偏差校验信息。
- 汇总层排除同一 BTC 持仓期间的重叠信号，并对独立触发样本小于 100 的结果标记低置信度。
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
   当前仍用固定滑点模拟成交，没有考虑盘口深度、成交量、部分成交和延迟；v4 已加入资金费率、标记价格强平和 maker/taker 手续费，但仍不等同于交易所撮合回放。

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

- 已落地：默认引擎版本升级为 `btc-plan-v5`，使用 `btc-execution-v1` 契约和闭合 K 线状态机，增加计划/成交质量门槛，并保存 K 线来源、周期、拉取时间、bar 数量、数据范围和哈希。
- 已落地：永续合约按标记价格和维持保证金率估算强平，累计资金费率，并区分 maker/taker 手续费。
- 已落地：增加前视偏差校验、低样本置信标记和单笔 R 倍数。
- 后续可继续扩展完整 K 线 payload 快照，支持离线复算和外部审计。

```text
r_multiple = net_pnl / risk_budget
```

### P1：更接近真实账户

- 使用动态权益 sizing：每笔交易按上一笔后的 equity 计算 risk_budget。
- 加入组合风险限制：同时未平仓计划的总风险不能超过账户上限。
- 使用交易所级保证金阶梯和风险限额替代固定维持保证金率。
- 增加盘口深度、部分成交和延迟模型。

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

## 13. 复评后的执行校准工具

分析生成阶段会读取近期已完成且已触发的 v5 结果，按 `horizon` 计算 MFE P50/P70，并在计划的 `target_calibration` 中标注目标距离是否超过同周期历史 P70。该标注只用于诊断，不改变方向、启用状态或校验结果；数据库不可用或样本不足时按无校准数据继续生成报告。

可用 `python scripts/evaluate_btc_direction_history.py` 对 `data/btc_okx_perpetual_1h_training.csv` 离线评估确定性 EMA/VWAP/Price Action 投票，并与恒定做多、上一根方向、固定种子随机基线比较。脚本只读 CSV，不写数据库；`--horizon-bars` 控制前瞻窗口，`--step` 可用于快速抽样，`--direction-threshold` 控制对称方向分数触发多空的绝对门槛。需要同时比较不同持有周期时使用 `--horizons 1,4,12,24`；输出会按每个窗口拆分信号覆盖率、MFE/MAE 机会命中率、下一根 K 线开盘到窗口末收盘的最终方向命中率、日线/小时线方向分项、毛收益、扣除每边手续费和滑点后的净收益、累计净收益、最大回撤和强度分桶。`MFE > MAE` 只表示窗口内曾出现过较大的有利波动，不能替代窗口末最终收益方向。净收益采用固定成本近似，不能替代 `CryptoBacktestEngine` 的真实撮合结果；不同 `step` 下信号可能重叠，累计收益仅用于序列诊断。

新生成计划会写入版本化 `tradeability_audit`，记录护栏是否执行、判定、原因以及护栏修改前的方向、入场、止损、目标和执行契约。对于被护栏改为等待的计划，回测使用同一批前向 K 线和同一 v5 执行引擎运行一次影子反事实评估，并写入结果 diagnostics；正式计划仍保持 `skipped`，影子交易不会进入账户权益、胜率、动态仓位或组合风险。总体汇总的 `guard_forward_comparison` 对比 `released_actual` 与 `blocked_counterfactual`，没有审计元数据的旧计划归入 `legacy_unclassified_count`，不按日期推断护栏版本。
