# BTC 多任务训练基线

本基线用于把 BTC 的研究目标从“预测价格点”收敛为“未来收益分布、波动和状态”。它是离线、观测用途的研究工具，不会改变报告方向、仓位、回测成交或真实交易开关。

## 运行

默认读取回填脚本生成的 `data/btc_okx_perpetual_1h_training.csv`：

```powershell
python scripts/train_btc_baseline.py
```

指定 5 分钟数据时，显式传入每个 horizon 的 bar 数：

```powershell
python scripts/train_btc_baseline.py --input data/btc_5m.csv --horizons 15m:3,1h:12,4h:48 --bar-hours 0.0833333
```

使用 `--output artifacts/btc-baseline.json` 可以保存 JSON 实验产物。输出包括最新 horizon 预测和折外评估结果。

阶段 1 模型对比和成本评估：

```powershell
python scripts/train_btc_baseline.py --model lightgbm --fee-bps-per-side 5 --slippage-bps-per-side 2 --output artifacts/btc-baseline-lightgbm.json
```

固定末段样本外评估和成本敏感性场景可以显式配置：

```powershell
python scripts/train_btc_baseline.py --holdout-bars 168 --cost-sensitivity-bps 14,30,50 --output artifacts/btc-baseline-holdout.json
```

`--model linear`（默认）使用 Ridge/Logistic，`--model lightgbm` 使用 LightGBM 回归器和分类器。两者都复用同一套特征、标签、purge 和 walk-forward 折，便于比较，不会自动选择或替换生产模型。

## 数据与标签

- `feature_*` 只使用当前及历史闭合 OHLCV 数据；标准化器在每个训练折内单独拟合。
- `target_return_*` 是未来窗口的对数收益率。
- `target_trade_return_*` 是按下一根 K 线开盘成交、在 horizon 末根 K 线收盘退出的对数收益率，仅用于执行口径评估。
- `target_volatility_*` 是未来窗口收益率 RMS。
- `target_direction_*` 按中性带分为 `down`、`neutral`、`up`，默认中性带为 0.2%。
- `target_regime_*` 分为 `trend_up`、`trend_down`、`high_volatility`、`sideways`。
- 当前支持可选的、已对齐的 `funding_rate`、`open_interest`、`basis`、`eth_close`、`sol_close`、`dxy_close`、`nasdaq_close` 和 `vix_close` 列；缺失时不会伪造中性值。

## 验证约束

每个 horizon 使用 purged expanding walk-forward：验证窗口前保留与该 horizon 等长的 purge gap，避免训练标签跨入验证期。除此之外，默认把最后 168 根已完成标签的 K 线固定为独立 holdout，并在 holdout 前保留同样长度的 purge 区间。holdout 模型只使用 purge 之前的样本拟合，最新 forecast 也复用该训练范围，不使用 holdout 标签；holdout 评估完成后不能用它调参或选择模型。

结果同时报告 walk-forward 折外和固定 holdout 的收益/波动误差、方向准确率、状态准确率、分位数 pinball loss，以及使用折外预测的成本后执行评估。固定 holdout 还会在同一批预测上重算 14、30、50 bps 等双边成本场景，不为每个成本重新训练模型。执行评估按 horizon（或显式 `--decision-stride`）抽取不重叠决策点，使用下一根开盘价到 horizon 末收盘价，扣除双边手续费和滑点，并输出信号数、执行方向命中率、净收益、累计收益、最大回撤和 profit factor。成本场景的信号门槛为 `abs(predicted_return) >= round_trip_cost + min_trade_edge`，因此成本升高时可能减少信号；这属于保守的成本覆盖诊断，不是撮合模拟。禁止使用随机 `train_test_split`。

数据不足以同时满足最小训练窗口、purge 和固定 holdout 长度时，holdout 会明确标记为 `insufficient`，不会静默缩短 holdout。`--holdout-bars 0` 可关闭固定 holdout，但此时结果不具备独立末段样本外证据。

该基线不保存模型文件，也不自动选择交易策略。执行评估只是一份 research 诊断，不是成交撮合器：没有盘口、部分成交、资金费率或强平模拟，也不会改变报告方向、仓位、回测成交或真实交易开关。只有在样本量、成本后表现、稳定性和风险约束都经过独立验证后，才应考虑把研究结果接入影子预测或策略层。
