---
kind: design
name: BTC 执行契约校验从降级改为标注模式
source: session
category: adr
---

# BTC 执行契约校验从降级改为标注模式

_来源：126af7f → eb0d1f3 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
原 align_btc_execution_plans 对校验失败的 BTC 计划直接改写 direction=wait 并将整体建议降级为观望，导致即使只是参数不够理想的计划也被强制禁止交易；同时 JSON 输出模板中的 execution_contract 范例写死固定值，LLM 照抄后产生过于苛刻且等待窗口极短的计划。

## 决策驱动
- 保留用户原始方向判断
- 让 LLM 生成的计划更具可触发性
- 避免过度保守的默认行为

## 备选方案
- **保持原有降级逻辑（失败即 wait）** _（已否决）_ — 优点：保守安全，减少误交易风险；缺点：误伤有效计划，降低信号触发率
- **仅标注不修改 direction** — 优点：保留分析结论，由下游或用户决定是否交易；错误码与双语说明便于诊断；缺点：下游若依赖 direction=wait 需适配读取新字段

## 决策
重写 align_btc_execution_plans：保留 validate_execution_plan 与 _validate_btc_execution_ladder 的检查逻辑，但对每个 plan 不再改写 direction 或写入 no_trade_reason，改为写入 validation_status（passed/failed）、validation_errors（错误码列表）和 validation_note（中英双语说明），回测引擎侧保持不变。

## 影响
报告产出侧不再自动将未通过校验的计划降为观望，下游消费方需关注 validation_status 而非仅读 direction；prompt 中新增入场价贴近现价（±1×ATR）、确认价偏离不超过 0.5%、止损不超过 1.5×ATR 等约束，配合解冻后的 execution_contract 示例（confirmation_bars=1、max_wait_bars 按周期匹配），预期提升计划可触发性并减少无效等待。