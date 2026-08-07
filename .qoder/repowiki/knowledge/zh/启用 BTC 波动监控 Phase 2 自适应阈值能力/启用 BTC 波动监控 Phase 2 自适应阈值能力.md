---
kind: design
name: 启用 BTC 波动监控 Phase 2 自适应阈值能力
source: session
category: adr
---

# 启用 BTC 波动监控 Phase 2 自适应阈值能力

_来源：126af7f → eb0d1f3 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
Phase 2 的自适应阈值、速度触发、分级确认代码已实现且测试全绿，但 .env 仅启用静态参数（WINDOW_MINUTES=1、THRESHOLD_PCT=1.0），未开启 WINDOW_TIERS、ADAPTIVE_THRESHOLD_ENABLED、VELOCITY_ENABLED、FAST_CONFIRMATION_ENABLED 等新开关。

## 决策驱动
- 利用已实现的自适应能力降低误报与漏报
- 本地定标而非硬编码默认值
- 保持向后兼容（不改 .env.example）

## 备选方案
- **保持静态阈值不变** _（已否决）_ — 优点：零变更风险；缺点：无法利用已实现的自适应机制，监控效果受限
- **通过 .env 启用 Phase 2 开关并按数据定标** — 优点：灵活可调，支持真实数据校准 tiers 与冷却时间；缺点：需要运行 replay_volatility_monitor.py --fetch --compare 进行定标

## 决策
在本地 .env 中启用 WINDOW_TIERS、ADAPTIVE_THRESHOLD_ENABLED、VELOCITY_ENABLED、FAST_CONFIRMATION_ENABLED，使用 scripts/replay_volatility_monitor.py --fetch 拉取 OKX BTC/USDT 近 3 天数据并通过 --compare 指标（检出率、延迟、误报率）定标各 tier 阈值与 COOLDOWN_MINUTES；网络不可用时回退到待办文件中预定的合成 fixture 推荐值。

## 影响
监控行为从单阈值静态判定切换为多窗口自适应判定，误报与漏报应随市场波动阶段动态调整；配置完全外置，可通过 revert .env 快速回滚至旧行为。