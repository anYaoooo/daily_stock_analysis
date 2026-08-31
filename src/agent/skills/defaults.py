# -*- coding: utf-8 -*-
"""
Shared defaults for trading skills.

This module centralises:
1. The default active skill set used by agent entrypoints
2. The fallback skill subset used by the multi-agent router
3. Common prompt fragments that previously drifted across multiple files
4. Helper utilities for skill-specific agent naming
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "strategies"

SKILL_AGENT_PREFIX = "skill_"
LEGACY_STRATEGY_AGENT_PREFIX = "strategy_"
SKILL_CONSENSUS_AGENT_NAME = "skill_consensus"
LEGACY_STRATEGY_CONSENSUS_AGENT_NAME = "strategy_consensus"

CORE_TRADING_SKILL_POLICY_ZH = """## 默认技能基线（必须严格遵守）

当前激活的 skills 可以补充细化分析视角，但默认风险控制和交易节奏必须遵守以下基线。

### 1. 严进策略（不追高）
- **绝对不追高**：当股价偏离 MA5 超过 5% 时，坚决不买入
- 乖离率 < 2%：最佳买点区间
- 乖离率 2-5%：可小仓介入
- 乖离率 > 5%：严禁追高！直接判定为"观望"

### 2. 趋势交易（顺势而为）
- **多头排列必须条件**：MA5 > MA10 > MA20
- 只做多头排列的股票，空头排列坚决不碰
- 均线发散上行优于均线粘合

### 3. 效率优先（筹码结构）
- 关注筹码集中度：90%集中度 < 15% 表示筹码集中
- 获利比例分析：70-90% 获利盘时需警惕获利回吐
- 平均成本与现价关系：现价高于平均成本 5-15% 为健康

### 4. 买点偏好（回踩支撑）
- **最佳买点**：缩量回踩 MA5 获得支撑
- **次优买点**：回踩 MA10 获得支撑
- **观望情况**：跌破 MA20 时观望

### 5. 风险排查重点
- 减持公告、业绩预亏、监管处罚、行业政策利空、大额解禁

### 6. 估值关注（PE/PB）
- PE 明显偏高时需在风险点中说明

### 7. 强势趋势股放宽
- 强势趋势股可适当放宽乖离率要求，轻仓追踪但需设止损
"""

CRYPTO_TWO_WAY_SKILL_POLICY_ZH = """## BTC 默认技能基线（必须严格遵守）

BTC 属于 7x24 双向交易标的，默认策略不得只选择多头。当前激活的 skills 可以补充细化分析视角，但交易计划必须同时覆盖多单与空单。

### 1. 双向策略选择
- 必须同时评估多单与空单，不得默认多头。
- 多单只在突破确认、回踩支撑确认、VWAP 上方强势或 EMA 结构偏多时作为主方案。
- 空单只在跌破关键支撑、反抽不过、VWAP 下方压制或 EMA 结构偏空时作为主方案。
- 多空条件都不充分时，输出等待/区间观察，不做多也不做空。

### 2. 策略点位要求
- 必须分别给出 `long_plan` 与 `short_plan`。
- 每套计划必须包含入场价、止损价、目标价、触发条件、失效条件和依据。
- 每套计划必须给出 `execution_ladder`：关键位的轻仓试仓、结构确认后的加仓，以及同一失效价触发后的撤销/退出。试仓不是无条件抄底或摸顶，必须基于回踩承接、扫流动性收回或区间边缘拒绝等明确结构。
- 当突破确认距离过远或风险收益不足时，优先给出可回测的回踩试仓方案；确认加仓必须是试仓后的动作，不能把追高/追空包装成更高概率信号。
- 最终主方案可以写入 `sniper_points` 兼容字段，但 `sniper_points` 不能替代双向计划。

### 2.1 `btc-right-side-v1` 双向状态机
- 扫高且收盘未站上前高时进入 `sweep_detected/short`；扫低且收盘未跌破前低时进入 `sweep_detected/long`。扫流动性只启动观察，不直接下市价单。
- 收盘越过方向对应的 `confirmation_price` 后，允许 `trial_entry` 试仓，默认最多使用计划仓位的 25%；没有反抽/回踩也不阻止受保护的小仓延续试仓。
- `confirmation_add` 必须在试仓方向验证后执行，且只有出现回踩/反抽确认（或明确的拒绝失败）才可加仓；反抽不是试仓的硬性前置条件。
- 当前价距离确认价超过 0.75×ATR，或已超过 `no_chase_price`，标记为机会错过并等待新结构，不继续使用已经远离的旧入场区间；状态最多等待 4 根对应周期 K 线。
- `breakdown_confirmed` 与 `breakout_confirmed` 对空、多方向对称适用，仍须检查失效价、风险收益比和追价边界。
- 急跌反弹使用独立的 `selloff_rebound_trial` 路径：事件形成时把事件低点所在 K 线高点冻结为试仓确认价，不得随着 EMA/VWAP 上移；短周期监控已确认时可直接使用计划仓位 25% 试仓，否则只需一根闭合小时线收于冻结确认价上方。量比仅影响仓位与置信度，不是试仓硬门槛；超过 0.5×ATR 禁止追价线或剩余风险收益比低于 1:1.5 时必须标记机会错过。

### 3. 点位贴近现价（可触发性约束）
- 入场价/试仓价必须落在当前价 ±1.0×ATR（日线 atr14）以内；若符合逻辑的点位距现价过远，不得给出遥远挂单，应改为等待条件并写明触发价位。
- 执行契约的确认价（close_above/close_below 的 value）必须贴近入场价，偏离不得超过 0.5%。
- 止损距离建议不超过 1.5×ATR；止损过宽时应缩小仓位而不是放宽止损。
- 上下文未提供 ATR 时，用近 14 根日线平均波幅估算，并在依据中说明。

### 4. 风控优先
- 禁止在指标冲突时激进追多或盲目开空。
- 任何方向都必须先定义失效条件，再给入场建议。
"""

CRYPTO_BATTLE_PLAN_SCHEMA_ZH = """## BTC 作战计划输出结构（必须严格遵守）

分析 BTC 时，`dashboard.battle_plan` 除 `sniper_points` 外必须同时输出以下结构，不得省略任一方向：

- `long_plan` 与 `short_plan`（日线级主策略，必须同时输出；即使倾向一边，另一边也要给“仅在何条件触发”的备用计划）：包含 `plan_type`、`direction`（long/short/wait）、`entry_zone`、`entry_price`、`stop_loss`、`take_profit`、`trigger_condition`、`invalidation`、`risk_reward`、`position_hint`、`confidence`、`reason`、`execution_ladder`、`execution_contract`；暂不满足时写清等待条件和 `no_trade_reason`。
- `intraday_plan`（仅承载小时线日内机会，不得混写日线主策略）：额外包含 `enabled` 与 `daily_constraint`；无日内机会时 `enabled=false`、`direction="wait"` 并写清等待条件。
- `execution_contract` 结构要求：`entry.setup_type` 取 breakout 或 pullback（突破跟随选 breakout，等回踩承接/反抽拒绝选 pullback）；`entry.conditions` 为 `{"type": ..., "value": ...}` 列表，普通 breakout 用 close_above/close_below 且必须带 volume_ratio_gte，pullback 用 low_lte/high_gte 触碰并搭配收盘确认；`entry.signal_class=selloff_rebound_trial` 时只用冻结确认价的 close_above、`confirmation_bars=1`，量比不作硬门槛；确认价 value 必须贴近 entry_price，偏离不超过 0.5%；`fill` 用 next_bar_open。
- 等待与持仓窗口：日线计划按日线K线计，`max_wait_bars` 在 2-5、`exit.max_holding_bars` 在 3-7 内按触发难易取值；日内计划按小时线计，`max_wait_bars` 不超过 8、`exit.max_holding_bars` 不超过 12。
- `execution_ladder`：关键位轻仓试仓、结构确认后加仓，以及同一失效价触发后的撤销/退出；试仓必须基于回踩承接、扫流动性收回或区间边缘拒绝等明确结构，并遵守 `btc-right-side-v1` 的 25% 试仓、0.75×ATR 禁止追价和 4 根 K 线等待窗口。
"""

TECHNICAL_SKILL_RULES_EN = """## Default Skill Baseline

Treat the currently activated skills as the primary analysis lens, but keep the
following default risk controls as the shared baseline:

- Bullish alignment: MA5 > MA10 > MA20
- Bias from MA5 < 2% -> ideal buy zone; 2-5% -> small position; > 5% -> no chase
- Shrink-pullback to MA5 is the preferred entry rhythm
- Below MA20 -> hold off unless the active skill explicitly proves a better setup
"""


def get_default_trading_skill_policy(*, explicit_skill_selection: bool) -> str:
    """Return the legacy default trading baseline only for implicit/default runs.

    When a caller explicitly chooses a skill (via request payload or config),
    analysis should follow that selected skill alone instead of silently
    layering the old bull-trend baseline on top.
    """
    if explicit_skill_selection:
        return ""
    return CORE_TRADING_SKILL_POLICY_ZH


def get_crypto_two_way_trading_skill_policy(*, explicit_skill_selection: bool) -> str:
    """Return the BTC two-way baseline only for implicit/default runs."""
    if explicit_skill_selection:
        return ""
    return CRYPTO_TWO_WAY_SKILL_POLICY_ZH


def get_crypto_battle_plan_prompt_section(*, explicit_skill_selection: bool) -> str:
    """Return the BTC prompt section for agent-mode runs.

    The battle-plan structure contract is always included because downstream
    execution validation depends on those fields; the two-way strategy baseline
    follows the same explicit-skill gate as the stock baseline.
    """
    sections = [
        get_crypto_two_way_trading_skill_policy(
            explicit_skill_selection=explicit_skill_selection,
        ),
        CRYPTO_BATTLE_PLAN_SCHEMA_ZH,
    ]
    return "\n\n".join(section for section in sections if section)


def get_default_technical_skill_policy(*, explicit_skill_selection: bool) -> str:
    """Return the technical-agent baseline only for implicit/default runs."""
    if explicit_skill_selection:
        return ""
    return TECHNICAL_SKILL_RULES_EN


@lru_cache(maxsize=1)
def _load_builtin_skill_catalog() -> tuple[object, ...]:
    try:
        from src.agent.skills.base import load_skills_from_directory

        return tuple(load_skills_from_directory(_BUILTIN_SKILLS_DIR))
    except Exception:
        return ()


def _coerce_priority(value: object, default: int = 100) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_available_ids(available_skill_ids: Optional[Iterable[str]]) -> List[str]:
    normalized: List[str] = []
    if available_skill_ids is None:
        return normalized
    for skill_id in available_skill_ids:
        if isinstance(skill_id, str):
            cleaned = skill_id.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
    return normalized


def _normalize_skill_inputs(
    skills: Optional[Iterable[object]],
    available_skill_ids: Optional[Iterable[str]] = None,
) -> tuple[List[object], List[str]]:
    normalized_available = _normalize_available_ids(available_skill_ids)

    if skills is None:
        return list(_load_builtin_skill_catalog()), normalized_available

    skill_pool: List[object] = []
    for item in skills:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned and cleaned not in normalized_available:
                normalized_available.append(cleaned)
            continue
        if item is not None:
            skill_pool.append(item)
    return skill_pool, normalized_available


def _sort_skill_pool(skills: Iterable[object]) -> List[object]:
    return sorted(
        skills,
        key=lambda skill: (
            _coerce_priority(getattr(skill, "default_priority", 100)),
            str(getattr(skill, "display_name", "") or getattr(skill, "name", "")),
            str(getattr(skill, "name", "")),
        ),
    )


def _iter_candidate_skills(
    skills: Optional[Iterable[object]],
    *,
    available_skill_ids: Optional[Iterable[str]] = None,
    user_invocable_only: bool = True,
) -> tuple[List[object], List[str]]:
    skill_pool, normalized_available = _normalize_skill_inputs(skills, available_skill_ids)
    available_lookup = set(normalized_available)

    candidates: List[object] = []
    for skill in _sort_skill_pool(skill_pool):
        skill_id = str(getattr(skill, "name", "")).strip()
        if not skill_id:
            continue
        if user_invocable_only and not bool(getattr(skill, "user_invocable", True)):
            continue
        if available_lookup and skill_id not in available_lookup:
            continue
        candidates.append(skill)

    return candidates, normalized_available


def _slice_skill_ids(skill_ids: List[str], max_count: Optional[int]) -> List[str]:
    if max_count is None:
        return skill_ids
    return skill_ids[:max_count]


def _pick_primary_default_skill_id(candidates: List[object]) -> str:
    preferred = [
        str(getattr(skill, "name", "")).strip()
        for skill in candidates
        if bool(getattr(skill, "default_active", False))
    ]
    if preferred:
        return preferred[0]

    fallback = [str(getattr(skill, "name", "")).strip() for skill in candidates]
    if fallback:
        return fallback[0]

    return ""


def get_default_active_skill_ids(
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    default_skill_id = _pick_primary_default_skill_id(candidates)
    if default_skill_id:
        return _slice_skill_ids([default_skill_id], max_count)

    return _slice_skill_ids(normalized_available[:1], max_count)


def get_default_router_skill_ids(
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    preferred = [
        str(getattr(skill, "name", "")).strip()
        for skill in candidates
        if bool(getattr(skill, "default_router", False))
    ]
    if preferred:
        return _slice_skill_ids(preferred, max_count)

    return get_default_active_skill_ids(
        candidates,
        max_count=max_count,
        available_skill_ids=normalized_available,
    )


def get_regime_skill_ids(
    regime: str,
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    regime_name = (regime or "").strip().lower()
    if regime_name:
        matched = []
        for skill in candidates:
            market_regimes = getattr(skill, "market_regimes", None) or []
            normalized_regimes = {
                str(item).strip().lower()
                for item in market_regimes
                if str(item).strip()
            }
            if regime_name in normalized_regimes:
                matched.append(str(getattr(skill, "name", "")).strip())
        if matched:
            return _slice_skill_ids(matched, max_count)

    return get_default_router_skill_ids(
        candidates,
        max_count=max_count,
        available_skill_ids=normalized_available,
    )


def get_primary_default_skill_id(
    skills: Optional[Iterable[object]] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> str:
    defaults = get_default_active_skill_ids(skills, max_count=1, available_skill_ids=available_skill_ids)
    return defaults[0] if defaults else ""


def _build_regime_skill_ids(skills: Iterable[object]) -> Dict[str, List[str]]:
    regime_map: Dict[str, List[str]] = {}
    for skill in _sort_skill_pool(skills):
        skill_id = str(getattr(skill, "name", "")).strip()
        if not skill_id:
            continue
        for regime in getattr(skill, "market_regimes", None) or []:
            regime_name = str(regime).strip().lower()
            if not regime_name:
                continue
            regime_map.setdefault(regime_name, []).append(skill_id)
    return regime_map


DEFAULT_ACTIVE_SKILL_IDS: tuple[str, ...] = tuple(get_default_active_skill_ids())
DEFAULT_ROUTER_SKILL_IDS: tuple[str, ...] = tuple(get_default_router_skill_ids())
PRIMARY_DEFAULT_SKILL_ID = get_primary_default_skill_id()
REGIME_SKILL_IDS: Dict[str, List[str]] = _build_regime_skill_ids(_load_builtin_skill_catalog())


def build_skill_agent_name(skill_id: str) -> str:
    return f"{SKILL_AGENT_PREFIX}{skill_id}"


def extract_skill_id(agent_name: Optional[str]) -> Optional[str]:
    if not agent_name or not isinstance(agent_name, str):
        return None
    for prefix in (SKILL_AGENT_PREFIX, LEGACY_STRATEGY_AGENT_PREFIX):
        if agent_name.startswith(prefix):
            return agent_name[len(prefix):]
    return None


def is_skill_agent_name(agent_name: Optional[str]) -> bool:
    return extract_skill_id(agent_name) is not None


def is_skill_consensus_name(agent_name: Optional[str]) -> bool:
    return agent_name in {SKILL_CONSENSUS_AGENT_NAME, LEGACY_STRATEGY_CONSENSUS_AGENT_NAME}
