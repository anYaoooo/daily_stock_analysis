# BTC 分析系统底层逻辑、交易结果与改进建议

报告日期：2026-08-26  
数据范围：本地 `data/stock_analysis.db`，BTC 分析记录 195 条，BTC 计划回测结果 236 条，当前引擎 `btc-plan-v5`。  
报告目的：解释系统到底如何分析 BTC、方向如何形成、价格和指标如何驱动计划、回测如何定义成交与亏损，并基于当前样本给出改进路线。

## 1. 执行摘要

当前系统不是一个经过训练并直接输出买卖概率的 BTC 预测模型。它是一个由以下部分组成的交易计划系统：

1. 从交易所获取闭合 K 线和部分衍生品/订单流数据。
2. 用确定性代码计算价格行为、Fibonacci、Volume、VWAP、EMA、ATR 和 EWMA 波动率。
3. 用日线和小时线形成背景方向与日内方向。
4. 将结构化行情上下文和规则送入 LLM，由 LLM 生成多空两套交易计划和执行条件。
5. 用确定性校验器检查计划的点位关系、风险收益比、成本覆盖、量能和执行契约。
6. 回测只评估这些已经生成的计划，不会从历史行情中重新搜索最佳信号。

当前结果说明系统暂时不具备实盘优势：

| 项目 | 当前值 | 正确解释 |
| --- | ---: | --- |
| 分析记录 | 195 | 报告数，不是交易数 |
| 计划评估 | 236 | 每条报告可包含日线多、日线空和小时线计划 |
| 已完成评估 | 152 | 排除跳过和等待数据后的完成数 |
| 形成信号 | 38 | 闭合 K 线满足执行条件的计划数 |
| 原始完整成交 | 12 | 尚未排除同一持仓期间的重叠信号 |
| 独立成交样本 | 9 | 汇总指标实际使用的成交分母 |
| 盈 / 亏 / 中性 | 2 / 5 / 2 | 独立成交样本 |
| 方向准确率 | 28.57% | 实际为 2 / 7，排除了 2 笔中性 |
| 总账户收益 | -1.2149% | 初始权益 10,000 的模拟账户口径 |
| Profit Factor | 0.5721 | 总盈利 / 总亏损绝对值，低于 1 表示亏损结构 |
| 平均单笔净收益 | -0.135% | 以账户初始权益为分母 |

最重要的判断是：低表现既有方向问题，也有执行和数据问题，但不能只用 28.57% 断言模型长期方向准确率。当前有效独立样本仅 9 笔，并且系统自己将其标记为低置信度（门槛 100 笔）。

## 2. 系统架构与数据流

```text
交易所行情 / 衍生品 / 订单流 / 新闻和宏观上下文
                 |
                 v
       闭合 K 线过滤和数据快照
                 |
                 v
      BTC 技术上下文构建器（确定性代码）
       |                         |
       | 日线                    | 小时线
       v                         v
  日线背景方向              独立日内方向
       \                         /
        v                       v
          多周期 alignment / 事件上下文
                         |
                         v
               LLM 交易计划生成
       long_plan / short_plan / intraday_plan
                         |
                         v
          btc-execution-v1 结构化执行契约
                         |
                         v
       v5 静态质量门 + 信号状态机 + 下一根开盘成交
                         |
                         v
       止盈/止损/持有期 + 手续费/滑点/资金费
                         |
                         v
       单笔结果、亏损归因、汇总和资金曲线
```

### 2.1 分析记录与交易计划不是一对一

历史报告来自 `analysis_history`。BTC 回测从报告的 `raw_result` 中抽取最多三类计划：

| 计划 | 周期 | 作用 |
| --- | --- | --- |
| `daily_long` | 日线 | 日线级做多主计划 |
| `daily_short` | 日线 | 日线级做空主计划 |
| `intraday` | 1 小时 | 日内独立机会，可多、空或等待 |

因此，一份报告可以同时存在两个日线候选和一个小时线候选。`195` 条报告产生 `236` 条计划评估，不应将其理解成 236 次独立下注。系统还会排除同一 BTC 持仓期间重叠的成交，避免同一段行情被重复计收益。

当前有 7 条分析记录没有对应的 `crypto_backtest_results`：`158、163、166、172、174、175、176`。这可能是没有可抽取计划、计划解析失败或尚未进入回测，应该在产品上显示为“未生成回测计划”，不能静默消失。

## 3. 行情数据和价格驱动

### 3.1 K 线口径

- 日线计划使用 daily K 线。
- 日内计划使用 hourly K 线。
- 技术指标只使用已闭合 K 线；正在形成的 K 线只能用于实时展示或触发上下文，不能直接作为已确认趋势。
- 回测从分析时点之后的未来 K 线开始，保存数据源、时间范围、bar 数和 SHA-256 哈希，并有 lookahead guard 检查前视偏差。
- 当前不是逐笔成交回放。K 线内先触发止盈还是止损通常不可知，同一根 K 线同时触发时采用保守的止损口径。

### 3.2 当前价格的主要驱动层

系统实际使用或要求 LLM 解释的价格驱动分为五层：

1. **结构和关键位**：近 20 根 K 线的前高、前低、支撑、阻力、swing high/low 和 Fibonacci 回撤位。
2. **趋势和均衡价格**：EMA20/EMA50 结构以及 rolling 20 VWAP 上下方位置。
3. **参与度和波动**：成交量相对过去均量的量比、ATR14、ATR14% 和 EWMA 下一根波动预测。
4. **事件和流动性**：真突破、真跌破、扫高、扫低、急跌、反弹确认、跌破延续。
5. **杠杆和短线订单流**：Funding、OI、基差、多空比、5 分钟主动买卖量/CVD；这些目前主要用于拥挤度、风控降权和执行确认，不应单独决定方向。

新闻和宏观相关性属于上下文解释层，不是经过回测证明的方向模型。在当前 236 条计划标签中，宏观相关性全部是 `unavailable`，因此不能把本轮盈亏归因给宏观事件。

## 4. 技术指标底层计算

实现位置主要是 `src/crypto_technical.py`，指标不是 LLM 自己计算的。

### 4.1 Price Action

系统先取最新闭合 K 线和此前 20 根 K 线：

- `close > 前 20 根最高价`：`breakout`。
- `close < 前 20 根最低价`：`breakdown`。
- 最高价扫过前高但收盘没有站上：`liquidity_sweep_high`，视为假突破/流动性掠夺风险。
- 最低价扫过前低但收盘没有跌破：`liquidity_sweep_low`，视为假跌破反弹候选。
- 实体和收盘明显上推：`bullish_push`。
- 实体和收盘明显下压：`bearish_push`。
- 以上都不满足：`range`。

核心原则是“影线越过不等于突破，必须看收盘是否站稳”。这能减少追逐单根插针，但也意味着信号天然较少。

### 4.2 Fibonacci

在当前 lookback 内取 swing high 和 swing low，计算：

```text
level = swing_high - (swing_high - swing_low) * ratio
ratio = 0.382 / 0.5 / 0.618
```

Fibonacci 主要用于回撤区、支撑阻力和目标位解释。当前代码没有证明 Fibonacci 单独具备预测能力，它是价格结构的一部分。

### 4.3 Volume

最新成交量除以前 20 根 K 线均量（排除最新 K 线）：

```text
volume_ratio = latest_volume / mean(previous_20_volume)
```

分类：

- `high`：量比 >= 1.5。
- `low`：量比 <= 0.7。
- `normal`：介于两者之间。

突破计划通常要求 `volume_ratio_gte`，最低配置为 1.0。回踩计划的量能主要影响置信度和仓位，不是必需硬门槛。

### 4.4 VWAP

使用最近 20 根 K 线的典型价格和成交量：

```text
typical_price = (high + low + close) / 3
rolling_vwap = sum(typical_price * volume) / sum(volume)
```

收盘价在 VWAP 上方标记 `above`，下方标记 `below`。它表达的是近期成交量加权的均衡价格，不是绝对趋势预测器。

### 4.5 EMA

计算 EMA20 和 EMA50：

- `close > EMA20 > EMA50`：`bullish`。
- `close < EMA20 < EMA50`：`bearish`。
- 其他情况：`mixed`。

EMA 反映趋势结构和价格位置，不能单独判断下一根 K 线涨跌。`mixed` 应被视为冲突或过渡，而不是中性利好。

### 4.6 ATR14

先计算 True Range：

```text
TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
ATR14 = mean(last_14_TR)
ATR14_pct = ATR14 / close * 100
```

ATR 用于判断止损是否落在日常噪音内、目标是否有足够空间。当前提示规则建议止损不超过约 1.5 ATR，但实际计划仍由 LLM 生成，再由 v5 检查几何和风险收益。

### 4.7 EWMA 波动率

使用闭合收益率和 lambda=0.94 的 EWMA 方差：

```text
sigma_t = sqrt(EWMA(return_t^2))
```

按历史 sigma 分位数分类：

| 状态 | 分位数 | 仓位上限 |
| --- | ---: | ---: |
| `compressed` | <= 10% | 不加杠杆，要求突破确认 |
| `normal` | 10% - 75% | 100% |
| `elevated` | >= 75% | 50% |
| `extreme` | >= 90% | 25% |

EWMA 只改变风险预算和仓位上限，不应直接改变多空方向。当前实际成交的 12 条完整样本中，波动预测标签全部缺失，说明该层没有真正参与这批成交的有效筛选。

### 4.8 衍生品、订单流和宏观

`CryptoDerivativesFetcher` 读取 Binance Futures 公共接口：

- Funding 当前值和 7 日历史：正值过高提示多头拥挤，负值过深提示空头拥挤和 short squeeze。
- OI 当前值和 24 小时变化：识别杠杆扩张、收缩和高名义规模。
- 永续基差：mark/index 的差值，区分 contango、backwardation、flat。
- 多空账户比：识别 long-heavy、short-heavy、balanced。
- 5 分钟 K 线主动买量和主动卖量：计算 taker buy ratio、CVD 和价格/CVD 背离。

CVD 的当前规则是执行影子确认：价格上涨但 CVD 偏空时降低追多置信度；价格下跌但 CVD 偏多时警惕假跌破。它不能单独把日线方向翻转。宏观相关性目前没有可用数据，不得把缺失当作中性。

## 5. 方向判定逻辑

### 5.1 确定性 bias 评分

`_infer_bias()` 的基础评分非常简单：

| 证据 | 多头分数 | 空头分数 |
| --- | ---: | ---: |
| EMA 结构 bullish / bearish | +2 | +2 |
| 价格在 VWAP 上方 / 下方 | +1 | +1 |
| Price Action bullish_push/breakout / bearish_push/breakdown | +1 | +1 |

总分较高的一方成为 `long` 或 `short`，相等则 `neutral`。这意味着系统的基础方向不是复杂的统计模型，而是“EMA 权重最高，VWAP 和价格行为辅助”的规则投票。

### 5.2 日线和小时线对齐

日线是主背景和风险边界，小时线是独立的日内机会层：

| 状态 | 含义 | 风控含义 |
| --- | --- | --- |
| `aligned_long` / `aligned_short` | 日线和小时线同向 | 优先级最高 |
| `countertrend_long` / `countertrend_short` | 小时线与日线相反 | 只能短线、轻仓、严格止损 |
| `wait_for_long_trigger` / `wait_for_short_trigger` | 日线有方向，小时线尚未确认 | 等小时线条件，不追价 |
| `hourly_only_wait_daily_confirmation` | 只有小时线有方向 | 低权重、短有效期 |
| `neutral` | 没有足够方向证据 | 观望 |

确定性 guard 只在日线和小时线同时反对某个日内方向时将其阻止；它允许小时线独立产生逆日线短线机会。这种设计增加了机会数量，也增加了逆势交易风险。当前 9 个独立成交中，亏损样本明显集中在小时线和事件确认不足的短线路径。

### 5.3 LLM 的真实角色

LLM 不是从原始行情训练出来的方向分类器。它接收已经计算好的指标、事件、价格位置、衍生品上下文和系统规则，负责：

- 解释多个指标是否共振或冲突。
- 同时生成 long、short 和 intraday 计划。
- 选择突破或回踩场景。
- 把支撑、阻力、入场、止损、止盈、失效和等待条件写成结构化 JSON。
- 在数据冲突或机会不足时输出 `wait`。

因此当前系统的方向优势取决于三件事：基础指标是否有预测力、提示词是否让 LLM 正确整合指标、以及确定性执行层是否筛掉不合理点位。LLM 文案通顺不代表方向有统计优势。

## 6. 交易计划和执行契约

所有可交易多空计划要求 `btc-execution-v1`：

- `instrument` 必须明确 OKX 永续、成交价类型、标记价类型和保证金模式。
- `entry.setup_type` 只能是 `breakout` 或 `pullback`。
- 条件只允许 `close_above`、`close_below`、`low_lte`、`high_gte`、`volume_ratio_gte/lte`、`close_above_vwap`、`close_below_vwap`。
- 条件逻辑固定为 `all`，必须连续满足 `confirmation_bars`。
- 成交方式固定为下一根 K 线开盘 `next_bar_open`。
- 超过 `max_wait_bars` 未触发，记为 `no_entry`。
- 退出由 `stop_loss`、`take_profit` 和 `max_holding_bars` 控制。

### 6.1 v5 质量门

计划在生成价和下一根实际成交价两个阶段检查：

1. 多单必须满足 `stop_loss < entry < take_profit`。
2. 空单必须满足 `take_profit < entry < stop_loss`。
3. 风险收益比至少为 `1:1.2`。
4. 目标空间必须覆盖双边手续费、滑点和 0.2% 中性区间。
5. 突破计划需要满足量能条件。
6. 下一根开盘跳空导致计划失真时，信号保留但委托拒绝。

这套门的优点是避免把明显不可交易的 LLM 点位算成盈利交易；缺点是如果 LLM 频繁给出低质量计划，系统会产生大量“信号出现但无法成交”的报告，用户会感觉系统没有行动能力。正确做法不是盲目降低门槛，而是将计划质量作为独立 KPI，反向改进计划生成。

## 7. 回测如何判定成交和亏损

### 7.1 成交状态机

```text
未形成条件
   -> no_entry
条件在闭合 K 线上连续满足
   -> signal_triggered
下一根开盘仍满足几何、成本和量能门
   -> filled / entry_triggered
下一根开盘不满足质量门
   -> rejected
```

### 7.2 出场

- 多单：`low <= stop_loss` 止损，`high >= take_profit` 止盈。
- 空单：`high >= stop_loss` 止损，`low <= take_profit` 止盈。
- 同一根 K 线同时触发时按保守的止损处理。
- 在最长持有 bars 内都未触发时，以窗口最后一根 close 退出。

### 7.3 仓位、成本和收益

默认参数：初始权益 10,000，单笔风险预算 1%，最大名义仓位 100%，杠杆 1 倍，手续费单边 5 bps，滑点单边 2 bps，maker 2 bps，taker 5 bps。

```text
risk_budget = initial_equity * 1%
risk_sized_qty = risk_budget / abs(entry - stop_loss)
max_qty = max_notional / entry
quantity = min(risk_sized_qty, max_qty)
net_pnl = gross_pnl - fees - funding_cost
net_return_pct = net_pnl / initial_equity * 100
```

当前仓位使用固定 `initial_equity`，不是严格按每笔交易后的权益复利；汇总也没有完整的组合保证金占用和同时持仓风险管理。这是回测与真实账户之间的重要差距。

### 7.4 胜负和方向准确率

净收益率 >= 0.2% 为 `win`，<= -0.2% 为 `loss`，中间为 `neutral`。方向准确率只统计已经成交且不是中性的样本：

```text
direction_accuracy = correct_non_neutral / non_neutral_filled
```

所以页面的“方向准确率”不是所有报告的方向预测准确率，也不是不含手续费的裸价格方向准确率，更不是未来所有 BTC K 线的上涨概率。

## 8. 当前 195 条报告的结果复盘

### 8.1 计划层统计

| 计划类型 | 总数 | 完成 | 显式信号 | 明确拒单 | 完整成交 | 盈 / 亏 / 中性 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `daily_long` | 24 | 21 | 1 | 1 | 0 | 0 / 0 / 0 |
| `daily_short` | 24 | 21 | 3 | 2 | 1 | 1 / 0 / 0 |
| `intraday` | 188 | 110 | 33 | 22 | 11 | 3 / 5 / 3 |
| 合计 | 236 | 152 | 37 | 25 | 12 | 4 / 5 / 3 |

这里的“显式信号”按数据库字段 `signal_triggered=true` 统计。数据库中还有结果 ID 3 这一条旧兼容记录：它的 `simulated_exit_reason=fill_quality_gate_rejected`，但 `signal_triggered` 和 `order_status` 为空；如果按回测引擎的兼容 fallback 读取，它会被视为额外 1 个信号/拒单。因此汇总 diagnostics 的有效口径是 38 个信号、26 个拒单，而数据库显式字段口径是 37 个信号、25 个拒单；这说明状态字段没有完全迁移一致，本身就是需要修复的数据质量问题。

汇总层进一步排除了 3 个重叠成交：历史记录 26 的 `daily_short`、历史记录 56 的 `intraday`、历史记录 80 的 `intraday`。因此正式汇总显示 9 个独立成交、2 胜、5 负、2 中性。

### 8.2 12 条完整成交明细

下表是数据库中 `eval_status=completed` 且 `entry_triggered=true` 的全部 12 条完整成交。净 PnL 已含手续费、滑点和资金费。

| 结果 ID | 历史 ID | 计划 | 方向 | 入场 -> 出场 | 退出 | 结果 | 净收益率 | 净 PnL |
| ---: | ---: | --- | --- | --- | --- | --- | ---: | ---: |
| 628 | 23 | intraday | long | 64494.1 -> 64850.0 | take_profit | win | 0.4104% | +41.04 |
| 1153 | 34 | intraday | short | 63605.4 -> 63707.4 | window_end | loss | -0.3002% | -30.02 |
| 1416 | 39 | intraday | long | 64035.2 -> 64252.8 | window_end | neutral | +0.1390% | +13.90 |
| 1545 | 44 | intraday | short | 63554.2 -> 64160.0 | stop_loss | loss | -1.0898% | -108.98 |
| 3313 | 26 | daily_short | short | 63955.0 -> 63503.5 | window_end | win | +0.2490% | +24.90 |
| 2050 | 54 | intraday | short | 63903.8 -> 63220.0 | take_profit | win | +0.9369% | +93.69 |
| 2192 | 56 | intraday | short | 63778.6 -> 63210.0 | take_profit | win | +0.7521% | +75.21 |
| 3502 | 81 | intraday | long | 63815.8 -> 63662.5 | window_end | loss | -0.3877% | -38.77 |
| 3503 | 80 | intraday | long | 64013.2 -> 63929.3 | window_end | neutral | -0.1866% | -18.66 |
| 3866 | 87 | intraday | long | 64378.8 -> 64561.5 | window_end | neutral | +0.1380% | +13.80 |
| 5984 | 107 | intraday | long | 64658.8 -> 64388.3 | stop_loss | loss | -0.5581% | -55.81 |
| 10665 | 113 | intraday | long | 64369.1 -> 64134.95 | stop_loss | loss | -0.5035% | -50.35 |

注：上表对部分窗口结束价使用 `window close` 或缩略展示，完整精确字段保存在数据库 `crypto_backtest_results` 的 `simulated_entry_price`、`simulated_exit_price` 和 `diagnostics_json` 中。表中 12 笔的原始结果为 4 胜、5 负、3 中性；当前汇总排除 3 笔与已有 BTC 持仓重叠的信号。

另有历史 ID 193 的一条开放窗口成交，结果为 `insufficient_data/provisional`，尚未纳入胜率和收益汇总。

### 8.3 5 条亏损成交逐笔分析

| 历史 ID | 方向 | 主要指标标签 | 退出 | 净 PnL | 诊断 |
| ---: | --- | --- | --- | ---: | --- |
| 34 | short | breakdown / EMA bearish / VWAP below / volume normal / aligned_short | window_end | -30.02 | 方向未延续，目标未达，最终窗口价对空单不利 |
| 44 | short | bearish_push / EMA bearish / VWAP below / volume normal / aligned_short | stop_loss | -108.98 | 看似指标共振，但入场后反转并触发止损，是最严重单笔亏损 |
| 81 | long | bearish_push / EMA bullish / VWAP above / volume low / countertrend_long | window_end | -38.77 | 逆日线短线、低量、价格行为偏空，确认不足 |
| 107 | long | bearish_push / EMA bullish / VWAP above / volume low / aligned_long | stop_loss | -55.81 | 日线/小时线名义同向，但低量和 bearish push 造成多单失败 |
| 113 | long | bearish_push / EMA mixed / VWAP below / volume low / hourly_only_wait_daily_confirmation | stop_loss | -50.35 | 方向背景不清、低量、VWAP 下方，仍然形成了多单成交 |

亏损结构不是单一错误：

- 3 笔明确触发止损，说明入场确认没有过滤掉后续反转。
- 2 笔未触发止损但在窗口结束时亏损，说明目标/持有期/入场时机不匹配。
- 5 笔亏损中至少 3 笔带 `volume=low` 或低量风险，低量不应被当作普通确认。
- 亏损样本中出现 `bearish_push`、`VWAP below`、`countertrend_long`、`hourly_only_wait_daily_confirmation` 等明显冲突标签，说明提示词约束没有完全转化为硬性的入场拒绝条件。

### 8.4 原始分析计划与实际结果对照

以下是五条亏损对应的原始 BTC 报告计划字段。它们展示了“报告语言看起来谨慎”与“结构化计划最终仍然成交”之间的落差。

| 历史 ID | 报告计划 | 计划入场 / 止损 / 止盈 | 报告关键理由 | 实际结果 |
| ---: | --- | --- | --- | --- |
| 34 | intraday short | 63610.2 / 64122.18 / 62580 | 小时线 PA、VWAP、EMA 偏空，但报告同时提示不要直接追空 | 下一根开盘 63605.4 成交，窗口结束 63707.4，-30.02 |
| 44 | intraday short | 63554.9 / 64160 / 62350 | 小时线三项偏空，等待反弹失败确认 | 成交后触发止损，-108.98 |
| 81 | intraday long | 63681.4 / 63294.12 / 64480 | 日线/小时线结构允许短线多，但报告标注逆势、低置信度 | 低量环境下窗口结束亏损，-38.77 |
| 107 | intraday long | 64653.9 / 64388.3 / 65026.6 | 报告提示价格接近目标、不追高，应等待回踩确认 | 下一根开盘仍成交，随后触发止损，-55.81 |
| 113 | intraday long | 64366.4 / 64134.95 / 64820 | 报告承认日线方向不清、量比偏低，仍给出小时线多单 | 2 根 K 线后止损，-50.35 |

这五条记录说明目前的主要断点在“建议语义”到“可执行状态”的转换：报告中写了等待、低置信度、低量或不追价，但只要 JSON 中仍是 `enabled=true`、`direction=long/short` 且执行契约成立，回测就会把它当作可成交计划。需要增加硬性 `tradeability_status` 或 `no_trade` gate，不能只依赖自然语言提醒。

### 8.5 被拒绝的信号

数据库中有 26 条记录带有 `fill_quality_gate_rejected`：

- 25 条包含 `risk_reward_below_minimum`，其中 5 条同时包含 `target_does_not_clear_cost_and_neutral_band`。
- 1 条（结果 ID 718）包含 `long_target_must_be_above_entry`。
- 明确 `order_status=rejected` 的记录为 25 条；结果 ID 3 的旧记录 `simulated_exit_reason` 已是拒单，但 `order_status` 为空，造成统计口径不一致。

拒单结果意味着“条件曾经出现，但下一根实际开盘价不值得按原计划交易”，不是方向判断成功，也不是实盘成交。拒单后平均有利波动约 0.768%，说明其中一部分确实错过了行情，但不能据此证明放宽质量门会盈利，因为拒单后的波动没有按真实成交成本和组合风险执行。

## 9. 当前系统为什么表现差

### 9.1 方向层问题

1. 基础 bias 评分过于粗：EMA 2 分、VWAP 1 分、Price Action 1 分，没有统计校准，也没有按行情状态动态权重。
2. EMA、VWAP、Price Action 可能同时描述同一个价格信息，表面上是三项共振，实际可能是同一风险的重复表达。
3. 小时线允许逆日线交易，但当前样本不足以证明逆势短线有优势；它扩大了计划数量，也扩大了错误路径。
4. `bearish_push + VWAP below + low volume` 等组合没有被硬性禁止做多，系统仍可能交给 LLM 生成计划。
5. 方向指标和收益指标没有分离。净收益会受到手续费、滑点、资金费和仓位影响，不适合作为唯一方向准确率。

### 9.2 计划层问题

1. 236 个评估只有 38 个信号，说明大量报告只是条件等待或不可交易计划。
2. 26 个信号在下一根开盘被质量门拒绝，主要是风险收益比不足，说明 LLM 生成的入场/止损/目标经常不匹配。
3. 日线多单 24 条只有 1 个信号且最终拒单，日线主策略几乎没有经过有效成交验证。
4. 小时线计划占 188/236，系统实际被短线机会主导，但小时级噪音和交易成本更高。

### 9.3 数据和回测层问题

1. 236 条计划的宏观相关性全部不可用。
2. 实际成交样本中 EWMA 波动预测和订单流标签基本缺失，风险覆盖层没有充分参与决策。
3. 仅使用 OHLC，无法知道同一根 K 线的真实路径。
4. 固定初始权益 sizing，不是严格复利。
5. 没有完整组合级同时持仓、保证金和总风险上限。
6. 旧记录存在字段状态不一致（结果 ID 3），会影响拒单统计。
7. 目前只有 9 笔独立成交，不能根据分组中 1-6 笔样本得出稳定结论。

## 10. 改进建议和验收指标

### P0：先修正测量和数据契约

1. **拆分四套指标**：
   - 方向预测：不含交易成本、按固定未来窗口计算 long/short 方向命中(使用 MFE 和 MAE 两个纯价格轨迹指标来评估“预测准确度”，只要信号发出后 MFE > MAE，即认定原始方向预测正确，完全剥离交易成本的干扰)。
   - 信号质量：满足条件的计划 / 所有可评估计划。
   - 执行质量：成交、拒单、滑点和拒单后机会分别统计。
   - 账户结果：净 PnL、Profit Factor、期望、回撤、费用占比。
2. 将页面“方向准确率”改为“成交后非中性净结果命中率”，同时新增真正的 `direction_accuracy_raw_pct`。
3. 所有结果统一填充 `signal_triggered`、`order_status`、`entry_triggered`，修复结果 ID 3 这类兼容状态。
4. 对 7 条没有结果的历史记录显示明确原因：无计划、解析失败、不可回测或尚未处理。
5. 当衍生品、订单流、EWMA 或宏观数据缺失时显示 `missing`，禁止解释成 `neutral`，并降低可交易等级。

验收标准：任何一个页面指标都能明确说明分母；同一条记录在 API、Web、数据库和报告中的状态一致。

### P1：把方向和执行分层

1. 先用纯规则基线和简单统计模型建立基准：恒定看多、上一根方向、EMA/VWAP/Price Action 投票，LLM 只能与基线比较，不能默认被视为预测器。
2. 方向模型输出概率和校准区间，不直接输出入场点位。点位由确定性模块根据结构、ATR、支撑阻力生成候选，再由风险门筛选。
3. 对以下组合直接降级为等待或只允许极轻仓：
   - 多单 + `bearish_push` + VWAP 下方。
   - 多单 + `volume=low` 且没有收盘突破确认。
   - 逆日线 + 小时线 `neutral` 或 `hourly_only_wait_daily_confirmation`。
   - 计划目标不足以覆盖费用、滑点、资金费和中性带。
4. 逆日线计划单独计分、单独限仓、单独设置更短有效期；在独立样本达到至少 100 笔之前不允许提升权重。
5. 日线和小时线计划分开做 walk-forward 统计，不要把短线和波段收益混成一个方向率。

### P2：改造计划质量和仓位

1. 把计划质量分成 `direction_score`、`location_score`、`risk_reward_score`、`execution_score`，不能只看 LLM 的 confidence 文案。
2. 目标位必须来自 ATR、前高/前低、Fibonacci 或事件空间，且明示目标到达的时间假设。
3. 计划生成时先计算费用覆盖阈值，再选择 entry/stop/target；不要先由 LLM 随意写点位再大量被拒单。
4. 改为动态权益 sizing，每笔用上一笔后的 equity 计算风险预算。
5. 增加组合风险上限、连续亏损冷却、日内最大亏损保护和波动异常熔断。
6. 费用敏感性分析至少覆盖手续费 5/10/15 bps、滑点 2/5/10 bps，确认策略不是只在理想成本下成立。

### P3：提升回测真实性和研究能力

1. 使用 1 分钟 K 线或交易所成交数据解决同根 K 线先止盈还是先止损的问题。
2. 引入订单簿深度、延迟、部分成交和实际撮合价格模型。
3. 保存完整 K 线 payload 和版本化指标快照，支持离线重放。
4. 使用严格 walk-forward：训练区、验证区、测试区按时间分离，禁止随机切分造成泄漏。
5. 为每个指标组合展示样本数、Wilson/Bootstrap 区间、基准差值和费用后收益。
6. 只有在至少 100 笔独立、跨多个市场状态的样本下，才允许自动降权或加权；单个盈利样本不能改变仓位。

### 建议的实盘准入门槛

在以下条件全部满足前保持 dry-run，不自动下单：

- 至少 100 笔独立成交，最好 200 笔以上。
- 测试集 Profit Factor > 1.2，且扣除保守费用和滑点后仍成立。
- 测试集期望值为正，Bootstrap 置信区间不长期跨过零。
- 日线、小时线、顺势、逆势分别有独立统计，不靠单一类别的少数盈利交易。
- 最大回撤、连续亏损和组合风险均满足事先设定的上限。
- 真实交易先手动确认，再半自动，小资金灰度，最后才考虑自动化。

## 11. 最终判断

当前系统已经超过“只输出一句涨跌判断”的阶段，具备结构化计划、闭合 K 线、执行门、手续费、滑点、资金费和回测审计骨架；但它还不是能证明 BTC 方向预测有效的交易系统。

当前最准确的产品定位是：

> 一个把 LLM 生成的 BTC 交易计划转成可审计回测、并帮助发现计划质量问题的研究工具。

它目前不能被描述为：

> 一个已经验证可以稳定预测 BTC 方向并自动盈利的系统。

要让系统真正变得有用，优先级不是继续增加更多指标或更换更强 LLM，而是：先把方向预测、计划质量、执行成交和净收益四个层次分开测量；再用足够大的无泄漏样本验证哪些指标组合确实有增量预测力；最后才把经过验证的组合接入仓位和交易执行。

## 12. 关键代码和数据位置

- 技术指标：`src/crypto_technical.py`
- BTC 分析提示词和计划校验：`src/analyzer.py`
- BTC 回测算法：`src/core/crypto_backtest_engine.py`
- 回测编排和亏损归因：`src/services/crypto_backtest_service.py`
- 衍生品和 CVD：`data_provider/crypto_derivatives_fetcher.py`
- 数据表：`src/storage.py` 中的 `analysis_history`、`crypto_backtest_results`、`crypto_backtest_summaries`
- 回测系统说明：[docs/btc-backtest-system.md](./btc-backtest-system.md)
