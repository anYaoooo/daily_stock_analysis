# BTC 分析可触发性改进

## 背景根因
- 计划无法触发：`src/analyzer.py` 的 JSON 输出模板把 `execution_contract` 范例写死（`setup_type=breakout`、`confirmation_bars=2`、`max_wait_bars=3`、固定条件组合），LLM 照抄导致计划苛刻且等待窗口极短；入场价无贴近现价的约束。
- 校验降级太狠：`align_btc_execution_plans`（src/analyzer.py L1751）对校验失败的计划直接改 `direction=wait` 并把整体建议降级为"观望"。
- 监控呆板：`.env` 只启用静态参数（WINDOW_MINUTES=1 + THRESHOLD_PCT=1.0），Phase 2 自适应阈值/速度触发/分级确认代码已实现且测试全绿，但开关未启用。

## 校验改为只标注（src/analyzer.py）
- 重写 `align_btc_execution_plans`：保留 `validate_execution_plan` + `_validate_btc_execution_ladder` 的检查逻辑，但对每个 plan（long_plan/short_plan/intraday_plan）不再修改 `direction`、不再写 `no_trade_reason`，改为写入：
  - `validation_status`: `"passed"` / `"failed"`
  - `validation_errors`: 错误码列表（沿用现有错误码）
  - `validation_note`: 中英双语可读说明（沿用现有文案生成逻辑，按 `result.report_language` 分支）
- 删除整体降级逻辑（operation_advice/decision_type/action 改"观望"的分支）。
- 非法 direction（非 long/short/wait）同样只标注 `validation_status=failed`，不改写原值。
- 不加配置开关（避免开关堆叠；用户已明确选择该策略）。
- 回测引擎 `validate_execution_plan` 本身不动：回测侧继续跳过无效计划，只改分析产出侧的呈现。

## Prompt 提升计划可触发性
- `src/agent/skills/defaults.py` 的 `CRYPTO_TWO_WAY_SKILL_POLICY_ZH` 增补"点位贴近现价"规则：
  - 入场价/试仓价必须落在当前价 ±1.0×ATR（日线）以内，无法给出时说明原因并改为等待条件。
  - 执行契约确认价（close_above/close_below value）与入场价偏离不得超过 0.5%。
  - 止损距离建议不超过 1.5×ATR。
  - ATR 数据已在 `crypto_technical` 上下文中（atr14/atr_pct），LLM 可见，无需新增数据管道。
- `src/analyzer.py` JSON 输出模板中 `execution_contract` 范例解冻：
  - `setup_type` 示例改为 `"breakout 或 pullback（按结构二选一）"`。
  - `confirmation_bars` 示例从 2 改为 1，注释说明可选 1-2。
  - `max_wait_bars` 从固定 3 改为按周期匹配的说明（日线计划可用至 24 根小时线窗口），多/空/日内三处同步。
  - conditions 列表标注"按 setup 选择"：pullback 用 `low_lte`/`high_gte` 触碰 + 收盘确认，breakout 用 close 突破 + `volume_ratio_gte`。
  - version/instrument/fill/order_type 等契约骨架保持不变。

## 监控自适应启用（本地配置，不改代码默认值）
- 用 `scripts/replay_volatility_monitor.py --fetch`（okx BTC/USDT 近 3 天）+ `--compare` 定标：检出率 / 检出延迟 / 误报率三指标权衡。
- 按定标结果更新 `.env`（用户本地，不改 `.env.example` 默认）：启用 `WINDOW_TIERS`、`ADAPTIVE_THRESHOLD_ENABLED`、`VELOCITY_ENABLED`、`FAST_CONFIRMATION_ENABLED`，必要时调整 `COOLDOWN_MINUTES`。
- 若网络拉取不可用，回退到待办文件已定标的合成 fixture 推荐值（tiers `1:0.4,3:0.7,5:1.0,15:1.5`），并在交付说明中注明未经真实数据定标。

## 测试
- `tests/test_btc_execution_alignment.py`：3 个降级断言用例改写为标注断言（direction 保留、`validation_status/validation_errors` 写入、operation_advice 不被改写）；保留有效计划用例与 ladder 一致性用例（断言改为错误码出现在 `validation_errors`）。
- 新增用例：校验失败时 `operation_advice`/`decision_type` 保持原值。
- 回归：`pytest tests/test_btc_execution_alignment.py tests/test_btc_volatility_monitor.py -q`；监控 7 个 legacy 用例不得修改。
- `python -m py_compile` 改动文件；时间允许跑 `pytest -m "not network"` 离线套件。

## 文档
- `docs/CHANGELOG.md` `[Unreleased]` 扁平格式追加：`- [改进] BTC 执行契约校验改为标注模式，不再将未通过校验的计划降级为观望`、`- [改进] BTC 双向计划 prompt 增加入场价贴近现价与等待窗口约束`。
- `docs/full-guide.md` / `full-guide_EN.md` 中执行契约/校验相关段落同步语义变化（评估双语同步）。

## 不做的事（范围外）
- "分析方向不准"的 prompt 质量/数据质量优化（需单独立项）。
- Phase 3 分析链路提速（intraday_fast 模式、触发预取快照、微回踩确认），待办文件要求单独评审。
- 回测引擎校验规则、报告模板渲染结构不改。

## 风险与回滚
- Prompt 模板与校验语义属用户可见行为变化：报告内容会保留未过校验的计划并带标注字段，下游若依赖"direction=wait 即不可交易"需改为读 `validation_status`（当前模板未消费该字段，无兼容破坏）。
- 回滚方式：`align_btc_execution_plans` 与 prompt 改动均为单文件局部改动，revert 对应 commit 即恢复旧降级行为；`.env` 自适应开关改回原值即可关闭监控新能力。