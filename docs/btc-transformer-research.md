# BTC Transformer 研究模块

本模块把 BTC 多模型方案拆成一个离线研究工具：同一份闭合 K 线特征可以分别训练 `PatchTST`、`iTransformer` 或 `fusion`（两个表征拼接），并对收益、波动率、方向和市场状态执行多任务预测。它不会写入交易计划，也不会参与报告方向、仓位、回测成交或真实交易。

## 运行

需要安装可选的 PyTorch 研究依赖（CPU 也可，不会进入默认生产镜像）：

```powershell
pip install -r requirements-ml.txt
python scripts/train_btc_transformer.py --architecture patchtst
python scripts/train_btc_transformer.py --architecture itransformer --output artifacts/btc-itransformer.json
python scripts/train_btc_transformer.py --architecture fusion --input data/btc_5m.csv --bar-hours 0.0833333 --horizons 15m:3,1h:12,4h:48
# GPU 机器可显式指定设备；默认仍为 CPU
python scripts/train_btc_transformer.py --architecture fusion --device cuda:0
```

默认参数是 `sequence_length=256`、`patch_length=16`、`stride=8`、`d_model=128`、`8` 个 attention heads、`3` 层 encoder，适合先做小规模实验。生产或长历史训练应显式指定设备、epoch、batch size，并保存外部实验产物。

## 组件

- `features.py`：从 OHLCV 及已对齐的 funding、OI、清算、订单簿和跨资产列生成因子；只使用当前及过去闭合 bar。
- `dataset.py`：将 `feature_*` 组织为 `(batch, sequence, features)`，标签包含 `15m/1h/4h` 收益、波动率、三分类方向和四分类 regime。
- `models.py`：提供 PatchTST（时间 patch token）、iTransformer（变量 token）和 representation fusion；每个 horizon 共享多任务 head。
- `trainer.py`：每个 walk-forward fold 独立拟合 scaler，保留 purge gap，最后才用全量历史拟合最新模型。输出折外误差和最新预测。

方向概率在输出前使用 softmax；训练器用验证折 logits 做温度校准并记录温度。`ensemble_forecasts` 是独立的组合工具，调用方应传入分别训练的 PatchTST、iTransformer、LightGBM 预测，且只对已校准、实际存在的模型归一化加权；不能把不同 horizon 当作模型直接平均。

## 数据与防泄漏

CSV 必须包含 `date/open/high/low/close/volume`。如果 DataFrame 的 `attrs["fetched_at"]` 存在，未完成的最后一根 bar 会被剔除。特征和标签在序列化前分开命名，随机切分被禁止；验证起点前的 purge 长度默认覆盖最长 `4h` horizon（5 分钟数据为 48 根）。

该模块仍是 research/shadow 产物。只有在固定训练区间、测试区间、交易成本和校准流程下完成独立比较后，才可讨论接入现有 shadow forecast；当前版本没有自动保存权重或下单能力。
