# BTC Transformer 研究模块

本模块把 BTC 多模型方案拆成一个离线研究工具：同一份闭合 K 线特征可以分别训练 `PatchTST`、`iTransformer` 或 `fusion`（两个表征拼接），并对收益、波动率、方向和市场状态执行多任务预测。它不会写入交易计划，也不会参与报告方向、仓位、回测成交或真实交易。

## 运行

需要安装可选的 PyTorch 研究依赖（CPU 也可，不会进入默认生产镜像）：

```powershell
pip install -r requirements-ml.txt
python scripts/train_btc_transformer.py --architecture patchtst
python scripts/train_btc_transformer.py --architecture itransformer --output artifacts/btc-itransformer.json
python scripts/train_btc_transformer.py --architecture fusion --input data/btc_5m.csv --bar-hours 0.0833333 --horizons 15m:3,1h:12,4h:48
# 交易成本较高时，可同步扩大方向标签的中性区间，减少噪声交易标签
python scripts/train_btc_transformer.py --architecture fusion --neutral-band-bps 35 --trading-cost-bps 25
# 极端行情较多时可收紧回归目标裁剪范围
python scripts/train_btc_transformer.py --architecture fusion --target-clip-sigma 4
# GPU 机器可显式指定设备；默认仍为 CPU
python scripts/train_btc_transformer.py --architecture fusion --device cuda:0
# 使用最新 CSV 做 cutoff 后样本外在线式验证；默认评估三种架构
python scripts/validate_btc_transformer_online.py --epochs 5 --output artifacts/btc-transformer-online-validation.json
```

默认参数是 `sequence_length=256`、`patch_length=16`、`stride=8`、`d_model=128`、`8` 个 attention heads、`3` 层 encoder，适合先做小规模实验。生产或长历史训练应显式指定设备、epoch、batch size，并保存外部实验产物。

JSON 结果会在 `training_config` 中记录本次实际使用的序列长度、模型规模、训练轮数、折数、purge 参数、设备和交易过滤参数；特征配置中的方向中性区间也应随交易成本通过 `--neutral-band-bps` 显式调整，便于比较不同资源配置下的实验结果。

训练默认对方向和 regime 分类按训练折的逆频率计算类别权重，避免模型只输出多数类，并提高方向任务在多任务损失中的权重。收益和波动率目标使用训练折内第 90 百分位绝对值做稳健尺度，超出 `--target-clip-sigma`（默认 5）时裁剪；评估和最新预测会还原到原始 log-return/volatility 单位，避免极端行情把回归头推到不可信的数值。收益-方向一致性约束默认关闭（`--direction-consistency-weight 0`），长周期数据可按实验结果再开启。训练窗口会随机打乱以改善优化，但验证仍保持时间顺序。每个验证 horizon 还会输出多数类方向基线、方向 Brier 分数，以及加入往返成本和最小收益缓冲后的交易统计（信号率、净收益、胜率、profit factor、最大回撤）。最新预测中的 `trade_signal` 只有在方向置信度、预期收益超过成本缓冲、以及收益回归与方向分类一致时才会给出 `long` 或 `short`，否则为 `hold` 并记录原因。

## 组件

- `features.py`：从 OHLCV 及已对齐的 funding、OI、清算、订单簿和跨资产列生成因子；只使用当前及过去闭合 bar。
- `dataset.py`：将 `feature_*` 组织为 `(batch, sequence, features)`，标签包含 `15m/1h/4h` 收益、波动率、三分类方向和四分类 regime。
- `models.py`：提供 PatchTST（时间 patch token）、iTransformer（变量 token）和 representation fusion；backbone 使用可学习 token pooling 保留关键时间片/变量，每个 horizon 共享多任务 head。
- `trainer.py`：每个 walk-forward fold 独立拟合 scaler，保留 purge gap，最后才用全量历史拟合最新模型。输出折外误差和最新预测。

方向概率在输出前使用 softmax；训练器用验证折 logits 做温度校准并记录温度。`ensemble_forecasts` 是独立的组合工具，调用方应传入分别训练的 PatchTST、iTransformer、LightGBM 预测，且只对已校准、实际存在的模型归一化加权；不能把不同 horizon 当作模型直接平均。

## 数据与防泄漏

CSV 必须包含 `date/open/high/low/close/volume`。如果 DataFrame 的 `attrs["fetched_at"]` 存在，未完成的最后一根 bar 会被剔除。特征和标签在序列化前分开命名，随机切分被禁止；验证起点前的 purge 长度默认覆盖最长 `4h` horizon（5 分钟数据为 48 根）。

该模块仍是 research/shadow 产物。交易统计是成本感知的离线诊断，不等同于可交易回测；只有在固定训练区间、测试区间、手续费、滑点、换手、基准策略和校准流程下完成独立比较后，才可讨论接入现有 shadow forecast。当前版本没有自动保存权重或下单能力。

`validate_btc_transformer_online.py` 会将最新闭合小时前 `--holdout-hours`（默认 24 小时）作为 cutoff，只用 cutoff 及之前可见的数据训练，再将 cutoff 后已经实现的 1/4/24 小时行情作为样本外结果。它不把未来标签混入训练，也不连接现有交易决策链路；运行前应先用 `scripts/backfill_btc_history.py --export-csv` 刷新 CSV。
