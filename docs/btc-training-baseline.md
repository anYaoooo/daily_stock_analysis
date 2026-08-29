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

## 数据与标签

- `feature_*` 只使用当前及历史闭合 OHLCV 数据；标准化器在每个训练折内单独拟合。
- `target_return_*` 是未来窗口的对数收益率。
- `target_volatility_*` 是未来窗口收益率 RMS。
- `target_direction_*` 按中性带分为 `down`、`neutral`、`up`，默认中性带为 0.2%。
- `target_regime_*` 分为 `trend_up`、`trend_down`、`high_volatility`、`sideways`。
- 当前支持可选的、已对齐的 `funding_rate`、`open_interest`、`basis`、`eth_close`、`sol_close`、`dxy_close`、`nasdaq_close` 和 `vix_close` 列；缺失时不会伪造中性值。

## 验证约束

每个 horizon 使用 purged expanding walk-forward：验证窗口前保留与该 horizon 等长的 purge gap，避免训练标签跨入验证期。结果同时报告 Ridge 收益/波动误差、方向准确率、状态准确率和分位数 pinball loss。禁止使用随机 `train_test_split`。

该基线不保存模型文件，也不自动选择交易策略。只有在样本量、成本后表现、稳定性和风险约束都经过独立验证后，才应考虑把研究结果接入影子预测或策略层。
