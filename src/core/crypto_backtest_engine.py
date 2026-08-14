# -*- coding: utf-8 -*-
"""BTC plan-level backtesting engine.

This module is deliberately DB-agnostic. It evaluates one extracted BTC
strategy plan against forward OHLC bars and returns serializable metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Protocol, Sequence

from src.schemas.crypto_instrument import resolve_crypto_instrument


class CryptoBarLike(Protocol):
    """Protocol for OHLC bars used by BTC plan backtests."""

    timestamp: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    volume_ratio: Optional[float]
    vwap: Optional[float]
    execution_open: Optional[float]
    execution_high: Optional[float]
    execution_low: Optional[float]
    execution_close: Optional[float]
    mark_open: Optional[float]
    mark_high: Optional[float]
    mark_low: Optional[float]
    mark_close: Optional[float]
    funding_rates: Sequence[float]
    funding_complete: bool


@dataclass(frozen=True)
class CryptoPlan:
    plan_type: str
    horizon: str
    direction: str
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    raw_plan: dict[str, Any]
    execution_contract: Optional[dict[str, Any]] = None
    position_multiplier_cap: float = 1.0


@dataclass(frozen=True)
class CryptoPlanBacktestConfig:
    neutral_band_pct: float = 0.2
    engine_version: str = "btc-plan-v2"
    initial_equity: float = 10000.0
    risk_per_trade_pct: float = 1.0
    max_notional_pct: float = 100.0
    leverage: float = 1.0
    fee_rate_bps: float = 5.0
    slippage_bps: float = 2.0
    maker_fee_rate_bps: float = 2.0
    taker_fee_rate_bps: float = 5.0
    maintenance_margin_rate: float = 0.005
    minimum_risk_reward: float = 1.2
    minimum_volume_ratio: float = 1.0


class CryptoBacktestEngine:
    """Evaluate long/short BTC plans using forward candles."""

    @classmethod
    def evaluate_plan(
        cls,
        *,
        plan: CryptoPlan,
        forward_bars: Sequence[CryptoBarLike],
        config: CryptoPlanBacktestConfig,
        evaluation_complete: bool = True,
    ) -> dict[str, Any]:
        if str(config.engine_version).strip().lower() in {"btc-plan-v3", "btc-plan-v4", "btc-plan-v5"}:
            return cls._evaluate_contract_plan(
                plan=plan,
                forward_bars=forward_bars,
                config=config,
                evaluation_complete=evaluation_complete,
            )

        return cls._evaluate_legacy_plan(plan=plan, forward_bars=forward_bars, config=config)

    @classmethod
    def validate_execution_plan(
        cls,
        *,
        plan: CryptoPlan,
        config: CryptoPlanBacktestConfig,
    ) -> list[str]:
        """Return the v5 execution checks used before a plan can be actionable."""

        direction = (plan.direction or "").strip().lower()
        if direction not in {"long", "short"}:
            return ["unsupported_direction"]
        if plan.entry_price is None or plan.entry_price <= 0:
            return ["missing_entry_price"]
        if plan.stop_loss is None or plan.take_profit is None:
            return ["missing_exit_prices"]

        contract, contract_errors = cls._validated_contract(
            plan.execution_contract,
            direction=direction,
        )
        if contract_errors:
            return [f"invalid_execution_contract:{error}" for error in contract_errors]

        if str(config.engine_version).strip().lower() != "btc-plan-v5":
            return []

        return cls._plan_quality_errors(
            plan=plan,
            contract=contract,
            entry_price=float(plan.entry_price),
            config=config,
            require_volume_gate=True,
        )

    @classmethod
    def _evaluate_legacy_plan(
        cls,
        *,
        plan: CryptoPlan,
        forward_bars: Sequence[CryptoBarLike],
        config: CryptoPlanBacktestConfig,
    ) -> dict[str, Any]:
        direction = (plan.direction or "").strip().lower()
        if direction not in {"long", "short"}:
            return cls._skipped(plan, "unsupported_direction")

        if plan.entry_price is None or plan.entry_price <= 0:
            return cls._skipped(plan, "missing_entry_price")

        window_bars = [bar for bar in forward_bars if cls._valid_bar(bar)]
        if not window_bars:
            return cls._insufficient(plan, "no_forward_bars")

        entry_index = cls._find_entry_index(window_bars, plan.entry_price)
        if entry_index is None:
            return {
                **cls._base(plan, eval_status="completed"),
                "outcome": "no_entry",
                "direction_correct": None,
                "start_price": plan.entry_price,
                "end_close": float(window_bars[-1].close),
                "max_high": cls._max_value(bar.high for bar in window_bars),
                "min_low": cls._min_value(bar.low for bar in window_bars),
                "simulated_entry_price": None,
                "simulated_exit_price": None,
                "simulated_exit_reason": "no_entry",
                "simulated_return_pct": None,
                "diagnostics": {
                    "reason": "entry_price_not_touched",
                    "execution": cls._execution_config(config),
                },
            }

        trade_bars = list(window_bars[entry_index:])
        end_close = float(trade_bars[-1].close)
        (
            hit_stop_loss,
            hit_take_profit,
            first_hit,
            first_hit_at,
            first_hit_bars,
            exit_price,
            exit_reason,
        ) = cls._evaluate_targets(
            direction=direction,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            trade_bars=trade_bars,
            end_close=end_close,
        )

        gross_return_pct = cls._directional_return_pct(
            direction=direction,
            entry_price=float(plan.entry_price),
            exit_price=exit_price,
        )
        trade = cls._simulate_trade(
            direction=direction,
            entry_price=float(plan.entry_price),
            exit_price=exit_price,
            stop_loss=plan.stop_loss,
            config=config,
            position_multiplier_cap=plan.position_multiplier_cap,
        )
        simulated_return_pct = trade["net_return_pct"]
        outcome, direction_correct = cls._classify_return(
            simulated_return_pct=simulated_return_pct,
            neutral_band_pct=config.neutral_band_pct,
        )

        return {
            **cls._base(plan, eval_status="completed"),
            "signal_triggered": True,
            "signal_triggered_at": trade_bars[0].timestamp,
            "order_status": "filled",
            "entry_triggered": True,
            "entry_triggered_at": trade_bars[0].timestamp,
            "start_price": float(plan.entry_price),
            "end_close": end_close,
            "max_high": cls._max_value(bar.high for bar in trade_bars),
            "min_low": cls._min_value(bar.low for bar in trade_bars),
            "direction_correct": direction_correct,
            "outcome": outcome,
            "hit_stop_loss": hit_stop_loss,
            "hit_take_profit": hit_take_profit,
            "first_hit": first_hit,
            "first_hit_at": first_hit_at,
            "first_hit_bars": first_hit_bars,
            "simulated_entry_price": float(plan.entry_price),
            "simulated_exit_price": exit_price,
            "simulated_exit_reason": exit_reason,
            "simulated_return_pct": simulated_return_pct,
            "diagnostics": {
                "evaluated_bars": len(window_bars),
                "trade_bars": len(trade_bars),
                "gross_return_pct": gross_return_pct,
                "execution": cls._execution_config(config),
                "trade": trade,
            },
        }

    @classmethod
    def _evaluate_contract_plan(
        cls,
        *,
        plan: CryptoPlan,
        forward_bars: Sequence[CryptoBarLike],
        config: CryptoPlanBacktestConfig,
        evaluation_complete: bool,
    ) -> dict[str, Any]:
        direction = (plan.direction or "").strip().lower()
        if direction not in {"long", "short"}:
            return cls._skipped(plan, "unsupported_direction")
        if plan.entry_price is None or plan.entry_price <= 0:
            return cls._skipped(plan, "missing_entry_price")
        if plan.stop_loss is None or plan.take_profit is None:
            return cls._skipped(plan, "missing_exit_prices")

        contract, errors = cls._validated_contract(plan.execution_contract, direction=direction)
        if errors:
            result = cls._skipped(plan, "invalid_execution_contract")
            result["diagnostics"]["contract_errors"] = errors
            return result

        engine_version = str(config.engine_version).strip().lower()
        is_perpetual_engine = engine_version in {"btc-plan-v4", "btc-plan-v5"}
        is_quality_gate_engine = engine_version == "btc-plan-v5"
        instrument = contract["instrument"]

        if is_quality_gate_engine:
            quality_errors = cls._plan_quality_errors(
                plan=plan,
                contract=contract,
                entry_price=float(plan.entry_price),
                config=config,
                require_volume_gate=True,
            )
            if quality_errors:
                result = cls._skipped(plan, "invalid_plan_quality")
                result["diagnostics"].update(
                    {
                        "quality_errors": quality_errors,
                        "execution_contract": contract,
                        "execution": cls._execution_config(config),
                    }
                )
                return result

        window_bars = [bar for bar in forward_bars if cls._valid_contract_bar(bar)]
        if not window_bars:
            return cls._insufficient(plan, "no_closed_forward_bars")
        if is_perpetual_engine and instrument.get("type") == "perpetual":
            if not all(cls._valid_perpetual_bar(bar) for bar in window_bars):
                result = cls._insufficient(plan, "incomplete_perpetual_trade_mark_data")
                result["diagnostics"]["execution_contract"] = contract
                return result
            if not all(bool(getattr(bar, "funding_complete", False)) for bar in window_bars):
                result = cls._insufficient(plan, "incomplete_funding_history")
                result["diagnostics"]["execution_contract"] = contract
                return result

        entry = contract["entry"]
        max_wait_bars = int(entry["max_wait_bars"])
        candidate_bars = window_bars[:max_wait_bars]
        trigger_index = cls._find_contract_trigger_index(
            bars=candidate_bars,
            conditions=entry["conditions"],
            confirmation_bars=int(entry["confirmation_bars"]),
            price_type=str(instrument.get("trigger_price_type") or "trade"),
        )
        if trigger_index is None:
            if not evaluation_complete and len(window_bars) < max_wait_bars:
                result = cls._insufficient(plan, "evaluation_window_open")
                result["diagnostics"].update(
                    {
                        "evaluated_bars": len(window_bars),
                        "execution_contract": contract,
                    }
                )
                return result
            return {
                **cls._base(plan, eval_status="completed"),
                "outcome": "no_entry",
                "direction_correct": None,
                "start_price": plan.entry_price,
                "end_close": cls._execution_price(window_bars[-1], "close"),
                "max_high": cls._max_value(bar.high for bar in window_bars),
                "min_low": cls._min_value(bar.low for bar in window_bars),
                "simulated_entry_price": None,
                "simulated_exit_price": None,
                "simulated_exit_reason": "conditions_not_met",
                "simulated_return_pct": None,
                "diagnostics": {
                    "reason": "entry_conditions_not_met",
                    "evaluated_bars": len(window_bars),
                    "execution_contract": contract,
                    "execution": cls._execution_config(config),
                },
            }

        fill_index = trigger_index + 1
        if fill_index >= len(window_bars):
            result = cls._insufficient(plan, "entry_confirmed_awaiting_fill_bar")
            signal_at = window_bars[trigger_index].timestamp
            result.update(
                {
                    "signal_triggered": True,
                    "signal_triggered_at": signal_at,
                    "order_status": "pending_fill",
                }
            )
            result["diagnostics"].update(
                {
                    "triggered_at": signal_at.isoformat(),
                    "execution_contract": contract,
                }
            )
            return result

        fill_bar = window_bars[fill_index]
        fill_price = cls._execution_price(fill_bar, "open")
        max_holding_bars = int(contract["exit"]["max_holding_bars"])
        available_trade_bars = list(window_bars[fill_index:])
        if is_quality_gate_engine:
            fill_quality_errors = cls._plan_quality_errors(
                plan=plan,
                contract=contract,
                entry_price=fill_price,
                config=config,
                require_volume_gate=False,
            )
            if fill_quality_errors:
                missed_moves = cls._missed_move_metrics(
                    direction=direction,
                    reference_price=fill_price,
                    bars=available_trade_bars[:max_holding_bars],
                )
                return {
                    **cls._base(plan, eval_status="completed"),
                    "signal_triggered": True,
                    "signal_triggered_at": window_bars[trigger_index].timestamp,
                    "order_status": "rejected",
                    "order_rejection_reason": ",".join(fill_quality_errors),
                    "entry_triggered": False,
                    "entry_triggered_at": None,
                    "outcome": "no_entry",
                    "direction_correct": None,
                    "start_price": fill_price,
                    "end_close": cls._execution_price(window_bars[-1], "close"),
                    "max_high": cls._max_value(bar.high for bar in window_bars),
                    "min_low": cls._min_value(bar.low for bar in window_bars),
                    "simulated_entry_price": None,
                    "simulated_exit_price": None,
                    "simulated_exit_reason": "fill_quality_gate_rejected",
                    "simulated_return_pct": None,
                    **missed_moves,
                    "diagnostics": {
                        "reason": "fill_quality_gate_rejected",
                        "triggered_at": window_bars[trigger_index].timestamp.isoformat(),
                        "rejected_fill_at": fill_bar.timestamp.isoformat(),
                        "rejected_fill_price": fill_price,
                        "quality_errors": fill_quality_errors,
                        "missed_move": missed_moves,
                        "execution_contract": contract,
                        "execution": cls._execution_config(config),
                    },
                }
        trade_bars = available_trade_bars[:max_holding_bars]
        end_close = cls._execution_price(trade_bars[-1], "close")
        target_result = cls._evaluate_targets(
            direction=direction,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            trade_bars=trade_bars,
            end_close=end_close,
        )
        (
            hit_stop_loss,
            hit_take_profit,
            first_hit,
            first_hit_at,
            first_hit_bars,
            exit_price,
            exit_reason,
        ) = target_result
        sizing = cls._position_sizing(
            entry_price=fill_price,
            stop_loss=plan.stop_loss,
            config=config,
            position_multiplier_cap=plan.position_multiplier_cap,
        )
        if is_perpetual_engine and instrument.get("type") == "perpetual":
            liquidation = cls._evaluate_liquidation(
                direction=direction,
                entry_price=fill_price,
                trade_bars=trade_bars,
                quantity=sizing["quantity"],
                margin_mode=str(instrument.get("margin_mode") or "isolated"),
                config=config,
            )
            if liquidation is not None:
                liquidation_at, liquidation_bars, liquidation_price = liquidation
                if first_hit_bars is None or liquidation_bars <= first_hit_bars:
                    first_hit = "liquidation"
                    first_hit_at = liquidation_at
                    first_hit_bars = liquidation_bars
                    exit_price = liquidation_price
                    exit_reason = "liquidation"
        holding_complete = len(available_trade_bars) >= max_holding_bars
        target_hit = exit_reason != "window_end"
        if not target_hit and not holding_complete and not evaluation_complete:
            return {
                **cls._base(plan, eval_status="insufficient_data"),
                "signal_triggered": True,
                "signal_triggered_at": window_bars[trigger_index].timestamp,
                "order_status": "filled",
                "entry_triggered": True,
                "entry_triggered_at": fill_bar.timestamp,
                "start_price": fill_price,
                "end_close": end_close,
                "max_high": cls._max_value(bar.high for bar in trade_bars),
                "min_low": cls._min_value(bar.low for bar in trade_bars),
                "direction_correct": None,
                "outcome": "provisional",
                "simulated_entry_price": fill_price,
                "simulated_exit_price": None,
                "simulated_exit_reason": "position_open",
                "simulated_return_pct": None,
                "diagnostics": {
                    "reason": "evaluation_window_open",
                    "evaluated_bars": len(window_bars),
                    "trade_bars": len(trade_bars),
                    "triggered_at": window_bars[trigger_index].timestamp.isoformat(),
                    "execution_contract": contract,
                    "execution": cls._execution_config(config),
                },
            }

        funding_bar_count = first_hit_bars or len(trade_bars)
        funding_cost = (
            cls._funding_cost(
                direction=direction,
                quantity=sizing["quantity"],
                trade_bars=trade_bars[:funding_bar_count],
            )
            if is_perpetual_engine and instrument.get("type") == "perpetual"
            else 0.0
        )
        trade = cls._simulate_trade(
            direction=direction,
            entry_price=fill_price,
            exit_price=exit_price,
            stop_loss=plan.stop_loss,
            config=config,
            sizing=sizing,
            funding_cost=funding_cost,
            entry_order_type=str(contract["entry"].get("order_type") or "market"),
            exit_order_type=str(contract["exit"].get("order_type") or "market"),
        )
        simulated_return_pct = trade["net_return_pct"]
        outcome, direction_correct = cls._classify_return(
            simulated_return_pct=simulated_return_pct,
            neutral_band_pct=config.neutral_band_pct,
        )
        return {
            **cls._base(plan, eval_status="completed"),
            "signal_triggered": True,
            "signal_triggered_at": window_bars[trigger_index].timestamp,
            "order_status": "filled",
            "entry_triggered": True,
            "entry_triggered_at": fill_bar.timestamp,
            "start_price": fill_price,
            "end_close": end_close,
            "max_high": cls._max_value(bar.high for bar in trade_bars),
            "min_low": cls._min_value(bar.low for bar in trade_bars),
            "direction_correct": direction_correct,
            "outcome": outcome,
            "hit_stop_loss": hit_stop_loss,
            "hit_take_profit": hit_take_profit,
            "first_hit": first_hit,
            "first_hit_at": first_hit_at,
            "first_hit_bars": first_hit_bars,
            "simulated_entry_price": fill_price,
            "simulated_exit_price": exit_price,
            "simulated_exit_reason": exit_reason,
            "simulated_return_pct": simulated_return_pct,
            "diagnostics": {
                "evaluated_bars": len(window_bars),
                "trade_bars": len(trade_bars),
                "triggered_at": window_bars[trigger_index].timestamp.isoformat(),
                "execution_contract": contract,
                "execution": cls._execution_config(config),
                "trade": trade,
            },
        }

    @staticmethod
    def _validated_contract(
        value: Any,
        *,
        direction: Optional[str] = None,
    ) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(value, dict):
            return {}, ["missing_execution_contract"]
        errors: list[str] = []
        if value.get("version") != "btc-execution-v1":
            errors.append("unsupported_contract_version")
        raw_instrument = value.get("instrument")
        instrument = resolve_crypto_instrument(
            raw_instrument or "BTC-USDT-PERP",
            default_type="perpetual",
            venue=(raw_instrument or {}).get("venue", "okx") if isinstance(raw_instrument, dict) else "okx",
            margin_mode=(raw_instrument or {}).get("margin_mode", "isolated") if isinstance(raw_instrument, dict) else "isolated",
        )
        if instrument is None:
            errors.append("invalid_instrument_contract")
        elif instrument.instrument_type == "spot" and str(direction or "").strip().lower() == "short":
            errors.append("spot_short_not_supported")
        elif instrument.trigger_price_type == "index":
            errors.append("index_trigger_price_not_supported")
        entry = value.get("entry")
        exit_config = value.get("exit")
        if not isinstance(entry, dict):
            errors.append("missing_entry_contract")
            entry = {}
        if not isinstance(exit_config, dict):
            errors.append("missing_exit_contract")
            exit_config = {}

        conditions = entry.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            errors.append("missing_entry_conditions")
            conditions = []
        supported = {
            "close_above",
            "close_below",
            "low_lte",
            "high_gte",
            "volume_ratio_gte",
            "volume_ratio_lte",
            "close_above_vwap",
            "close_below_vwap",
        }
        normalized_conditions: list[dict[str, Any]] = []
        for condition in conditions:
            if not isinstance(condition, dict):
                errors.append("invalid_entry_condition")
                continue
            condition_type = str(condition.get("type") or "").strip().lower()
            if condition_type not in supported:
                errors.append(f"unsupported_condition:{condition_type or 'missing'}")
                continue
            normalized = {"type": condition_type}
            if condition_type in {
                "close_above",
                "close_below",
                "low_lte",
                "high_gte",
                "volume_ratio_gte",
                "volume_ratio_lte",
            }:
                try:
                    normalized["value"] = float(condition.get("value"))
                except (TypeError, ValueError):
                    errors.append(f"missing_condition_value:{condition_type}")
                    continue
            normalized_conditions.append(normalized)

        if entry.get("logic", "all") != "all":
            errors.append("unsupported_entry_logic")
        if entry.get("fill", "next_bar_open") != "next_bar_open":
            errors.append("unsupported_fill_mode")
        entry_order_type = str(entry.get("order_type") or "market").strip().lower()
        exit_order_type = str(exit_config.get("order_type") or "market").strip().lower()
        if entry_order_type not in {"market", "limit"}:
            errors.append("unsupported_entry_order_type")
        if exit_order_type not in {"market", "limit"}:
            errors.append("unsupported_exit_order_type")
        try:
            confirmation_bars = int(entry.get("confirmation_bars", 1))
            max_wait_bars = int(entry.get("max_wait_bars", 24))
            max_holding_bars = int(exit_config.get("max_holding_bars", 24))
        except (TypeError, ValueError):
            errors.append("invalid_bar_limit")
            confirmation_bars, max_wait_bars, max_holding_bars = 1, 24, 24
        setup_type = str(entry.get("setup_type") or value.get("setup_type") or "breakout").strip().lower()
        if setup_type not in {"breakout", "pullback"}:
            errors.append("unsupported_setup_type")
            setup_type = "breakout"
        if not 1 <= confirmation_bars <= 3:
            errors.append("confirmation_bars_out_of_range")
        if max_wait_bars < 1 or max_holding_bars < 1:
            errors.append("bar_limit_must_be_positive")

        normalized_contract = {
            "version": "btc-execution-v1",
            "instrument": instrument.to_contract() if instrument is not None else {},
            "entry": {
                "setup_type": setup_type,
                "logic": "all",
                "conditions": normalized_conditions,
                "confirmation_bars": confirmation_bars,
                "fill": "next_bar_open",
                "order_type": entry_order_type,
                "max_wait_bars": max_wait_bars,
            },
            "exit": {
                "max_holding_bars": max_holding_bars,
                "order_type": exit_order_type,
            },
        }
        return normalized_contract, errors

    @classmethod
    def _plan_quality_errors(
        cls,
        *,
        plan: CryptoPlan,
        contract: dict[str, Any],
        entry_price: float,
        config: CryptoPlanBacktestConfig,
        require_volume_gate: bool,
    ) -> list[str]:
        """Validate that a triggered plan remains executable after costs and gaps."""

        direction = str(plan.direction or "").strip().lower()
        stop_loss = float(plan.stop_loss or 0.0)
        take_profit = float(plan.take_profit or 0.0)
        errors: list[str] = []
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            return ["non_positive_plan_price"]

        if direction == "long":
            risk_distance = entry_price - stop_loss
            reward_distance = take_profit - entry_price
            if risk_distance <= 0:
                errors.append("long_stop_must_be_below_entry")
            if reward_distance <= 0:
                errors.append("long_target_must_be_above_entry")
        else:
            risk_distance = stop_loss - entry_price
            reward_distance = entry_price - take_profit
            if risk_distance <= 0:
                errors.append("short_stop_must_be_above_entry")
            if reward_distance <= 0:
                errors.append("short_target_must_be_below_entry")

        if risk_distance > 0 and reward_distance > 0:
            risk_reward = reward_distance / risk_distance
            if risk_reward < max(float(config.minimum_risk_reward), 0.0):
                errors.append("risk_reward_below_minimum")

            gross_target_return_pct = reward_distance / entry_price * 100.0
            net_target_return_pct = gross_target_return_pct - cls._estimated_round_trip_cost_pct(
                contract=contract,
                config=config,
            )
            if net_target_return_pct < max(float(config.neutral_band_pct), 0.0):
                errors.append("target_does_not_clear_cost_and_neutral_band")

        setup_type = str((contract.get("entry") or {}).get("setup_type") or "breakout")
        condition_types = {
            str(condition.get("type") or "")
            for condition in (contract.get("entry") or {}).get("conditions") or []
        }
        if setup_type == "pullback":
            required_touch = "low_lte" if direction == "long" else "high_gte"
            close_confirmations = (
                {"close_above", "close_above_vwap"}
                if direction == "long"
                else {"close_below", "close_below_vwap"}
            )
            if required_touch not in condition_types:
                errors.append("missing_pullback_touch_condition")
            if not condition_types.intersection(close_confirmations):
                errors.append("missing_pullback_close_confirmation")
        elif require_volume_gate:
            volume_thresholds = [
                float(condition["value"])
                for condition in (contract.get("entry") or {}).get("conditions") or []
                if condition.get("type") == "volume_ratio_gte" and condition.get("value") is not None
            ]
            minimum_volume_ratio = max(float(config.minimum_volume_ratio), 0.0)
            if not volume_thresholds:
                errors.append("missing_volume_confirmation")
            elif max(volume_thresholds) < minimum_volume_ratio:
                errors.append("volume_confirmation_below_minimum")

        return errors

    @staticmethod
    def _estimated_round_trip_cost_pct(
        *,
        contract: dict[str, Any],
        config: CryptoPlanBacktestConfig,
    ) -> float:
        entry = contract.get("entry") or {}
        exit_config = contract.get("exit") or {}
        maker_bps = max(float(config.maker_fee_rate_bps), 0.0)
        taker_bps = max(float(config.taker_fee_rate_bps), 0.0)
        entry_fee_bps = maker_bps if entry.get("order_type") == "limit" else taker_bps
        exit_fee_bps = maker_bps if exit_config.get("order_type") == "limit" else taker_bps
        slippage_bps = max(float(config.slippage_bps), 0.0)
        return (entry_fee_bps + exit_fee_bps + 2 * slippage_bps) / 100.0

    @classmethod
    def _find_contract_trigger_index(
        cls,
        *,
        bars: Sequence[CryptoBarLike],
        conditions: Sequence[dict[str, Any]],
        confirmation_bars: int,
        price_type: str = "trade",
    ) -> Optional[int]:
        consecutive = 0
        for index, bar in enumerate(bars):
            if all(cls._condition_matches(bar, condition, price_type=price_type) for condition in conditions):
                consecutive += 1
                if consecutive >= confirmation_bars:
                    return index
            else:
                consecutive = 0
        return None

    @classmethod
    def _condition_matches(
        cls,
        bar: CryptoBarLike,
        condition: dict[str, Any],
        *,
        price_type: str = "trade",
    ) -> bool:
        condition_type = condition["type"]
        if price_type == "mark":
            mark_close = getattr(bar, "mark_close", None)
            if mark_close is None:
                return False
            close = float(mark_close)
        else:
            execution_close = getattr(bar, "execution_close", None)
            close = float(execution_close if execution_close is not None else bar.close)
        if condition_type == "close_above":
            return close > float(condition["value"])
        if condition_type == "close_below":
            return close < float(condition["value"])
        if condition_type == "low_lte":
            return cls._execution_price(bar, "low") <= float(condition["value"])
        if condition_type == "high_gte":
            return cls._execution_price(bar, "high") >= float(condition["value"])
        if condition_type == "volume_ratio_gte":
            return bar.volume_ratio is not None and float(bar.volume_ratio) >= float(condition["value"])
        if condition_type == "volume_ratio_lte":
            return bar.volume_ratio is not None and float(bar.volume_ratio) <= float(condition["value"])
        if condition_type == "close_above_vwap":
            return bar.vwap is not None and close > float(bar.vwap)
        if condition_type == "close_below_vwap":
            return bar.vwap is not None and close < float(bar.vwap)
        return False

    @classmethod
    def compute_summary(
        cls,
        *,
        results: Iterable[Any],
        scope: str,
        code: Optional[str],
        engine_version: str,
    ) -> dict[str, Any]:
        rows = list(results)
        completed = [row for row in rows if getattr(row, "eval_status", None) == "completed"]
        signal_triggered = [row for row in completed if cls._row_signal_triggered(row)]
        rejected_orders = [row for row in signal_triggered if cls._row_order_status(row) == "rejected"]
        raw_triggered = [row for row in completed if getattr(row, "entry_triggered", None) is True]
        structured_engine = str(engine_version).strip().lower() in {"btc-plan-v3", "btc-plan-v4", "btc-plan-v5"}
        if structured_engine:
            triggered, overlap_excluded = cls._independent_triggered_rows(raw_triggered)
        else:
            triggered, overlap_excluded = raw_triggered, []
        independent_ids = {id(row) for row in triggered}
        wins = [row for row in triggered if getattr(row, "outcome", None) == "win"]
        losses = [row for row in triggered if getattr(row, "outcome", None) == "loss"]
        neutral = [row for row in triggered if getattr(row, "outcome", None) == "neutral"]
        no_entry = [row for row in completed if getattr(row, "outcome", None) == "no_entry"]
        skipped = [row for row in rows if getattr(row, "eval_status", None) == "skipped"]
        insufficient = [row for row in rows if getattr(row, "eval_status", None) == "insufficient_data"]

        correct_rows = [row for row in triggered if getattr(row, "direction_correct", None) is not None]
        correct = sum(1 for row in correct_rows if getattr(row, "direction_correct", None) is True)
        win_loss_denominator = len(wins) + len(losses)

        by_plan_type: dict[str, dict[str, Any]] = {}
        for row in rows:
            plan_type = str(getattr(row, "plan_type", "") or "unknown")
            bucket = by_plan_type.setdefault(
                plan_type,
                {
                    "total": 0,
                    "completed": 0,
                    "signal_triggered": 0,
                    "orders_rejected": 0,
                    "triggered": 0,
                    "wins": 0,
                    "losses": 0,
                },
            )
            bucket["total"] += 1
            if getattr(row, "eval_status", None) == "completed":
                bucket["completed"] += 1
            if cls._row_signal_triggered(row):
                bucket["signal_triggered"] += 1
            if cls._row_order_status(row) == "rejected":
                bucket["orders_rejected"] += 1
            if id(row) in independent_ids:
                bucket["triggered"] += 1
            if id(row) in independent_ids and getattr(row, "outcome", None) == "win":
                bucket["wins"] += 1
            if id(row) in independent_ids and getattr(row, "outcome", None) == "loss":
                bucket["losses"] += 1

        for bucket in by_plan_type.values():
            denom = bucket["wins"] + bucket["losses"]
            bucket["win_rate_pct"] = round(bucket["wins"] / denom * 100, 2) if denom else None

        returns = [
            cls._diagnostic_number(row, "trade", "net_return_pct")
            for row in triggered
        ]
        r_multiples = [
            cls._diagnostic_number(row, "trade", "r_multiple")
            for row in triggered
        ]
        pnl_values = [
            cls._diagnostic_number(row, "trade", "net_pnl")
            for row in triggered
        ]
        total_fees = sum(
            value
            for value in (
                cls._diagnostic_number(row, "trade", "total_fee") for row in triggered
            )
            if value is not None
        )
        total_funding_cost = sum(
            value
            for value in (
                cls._diagnostic_number(row, "trade", "funding_cost") for row in triggered
            )
            if value is not None
        )
        equity_curve = cls._equity_curve(triggered)
        risk_metrics = cls._portfolio_risk_metrics(
            returns=[value for value in returns if value is not None],
            r_multiples=[value for value in r_multiples if value is not None],
            pnl_values=[value for value in pnl_values if value is not None],
            equity_curve=equity_curve,
            initial_equity=cls._summary_initial_equity(triggered),
            total_fees=total_fees,
            total_funding_cost=total_funding_cost,
        )
        sample_confidence = cls._sample_confidence(
            sample_count=len(triggered),
            minimum_sample_count=100 if structured_engine else 30,
        )

        return {
            "scope": scope,
            "code": code,
            "engine_version": engine_version,
            "total_evaluations": len(rows),
            "completed_count": len(completed),
            "triggered_count": len(triggered),
            "no_entry_count": len(no_entry),
            "skipped_count": len(skipped),
            "insufficient_count": len(insufficient),
            "win_count": len(wins),
            "loss_count": len(losses),
            "neutral_count": len(neutral),
            "direction_accuracy_pct": round(correct / len(correct_rows) * 100, 2) if correct_rows else None,
            "win_rate_pct": round(len(wins) / win_loss_denominator * 100, 2) if win_loss_denominator else None,
            "avg_simulated_return_pct": cls._average(
                getattr(row, "simulated_return_pct", None) for row in triggered
            ),
            "plan_type_breakdown": by_plan_type,
            "risk_metrics": risk_metrics,
            "equity_curve": equity_curve,
            "diagnostics": {
                "sample_confidence": sample_confidence,
                "signal_triggered_count": len(signal_triggered),
                "rejected_order_count": len(rejected_orders),
                "order_fill_rate_pct": (
                    round(len(raw_triggered) / len(signal_triggered) * 100, 2)
                    if signal_triggered
                    else None
                ),
                "avg_missed_favorable_move_pct": cls._average(
                    getattr(row, "missed_favorable_move_pct", None)
                    for row in rejected_orders
                ),
                "metric_semantics": (
                    "structured_execution_contract"
                    if structured_engine
                    else "entry_price_touch_proxy"
                ),
                "raw_triggered_count": len(raw_triggered),
                "overlap_excluded_count": len(overlap_excluded),
            },
        }

    @staticmethod
    def _row_signal_triggered(row: Any) -> bool:
        value = getattr(row, "signal_triggered", None)
        if value is not None:
            return value is True
        if getattr(row, "entry_triggered", None) is True:
            return True
        return getattr(row, "simulated_exit_reason", None) == "fill_quality_gate_rejected"

    @staticmethod
    def _row_order_status(row: Any) -> str:
        value = str(getattr(row, "order_status", None) or "").strip().lower()
        if value:
            return value
        if getattr(row, "entry_triggered", None) is True:
            return "filled"
        if getattr(row, "simulated_exit_reason", None) == "fill_quality_gate_rejected":
            return "rejected"
        return "not_triggered"

    @staticmethod
    def _independent_triggered_rows(rows: Sequence[Any]) -> tuple[list[Any], list[Any]]:
        accepted: list[Any] = []
        excluded: list[Any] = []
        active_until_by_code: dict[str, datetime] = {}
        ordered = sorted(
            rows,
            key=lambda row: (
                getattr(row, "entry_triggered_at", None) or datetime.max,
                getattr(row, "analysis_created_at", None) or datetime.max,
            ),
        )
        for row in ordered:
            entry_at = getattr(row, "entry_triggered_at", None)
            code = str(getattr(row, "code", "") or "unknown")
            if entry_at is None:
                excluded.append(row)
                continue
            active_until = active_until_by_code.get(code)
            if active_until is not None and entry_at < active_until:
                excluded.append(row)
                continue
            exit_at = getattr(row, "first_hit_at", None) or getattr(row, "evaluation_end", None) or entry_at
            accepted.append(row)
            active_until_by_code[code] = max(entry_at, exit_at)
        return accepted, excluded

    @staticmethod
    def _base(plan: CryptoPlan, *, eval_status: str) -> dict[str, Any]:
        if eval_status == "insufficient_data":
            order_status = "not_evaluated"
        elif eval_status == "skipped":
            order_status = "not_applicable"
        else:
            order_status = "not_triggered"
        return {
            "plan_type": plan.plan_type,
            "horizon": plan.horizon,
            "direction": plan.direction,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "position_multiplier_cap": plan.position_multiplier_cap,
            "eval_status": eval_status,
            "signal_triggered": False,
            "signal_triggered_at": None,
            "order_status": order_status,
            "order_rejection_reason": None,
            "entry_triggered": False,
            "entry_triggered_at": None,
            "missed_favorable_move_pct": None,
            "missed_adverse_move_pct": None,
        }

    @classmethod
    def _skipped(cls, plan: CryptoPlan, reason: str) -> dict[str, Any]:
        return {
            **cls._base(plan, eval_status="skipped"),
            "outcome": "skipped",
            "direction_correct": None,
            "entry_triggered": False,
            "diagnostics": {"reason": reason},
        }

    @classmethod
    def _insufficient(cls, plan: CryptoPlan, reason: str) -> dict[str, Any]:
        return {
            **cls._base(plan, eval_status="insufficient_data"),
            "outcome": "insufficient_data",
            "direction_correct": None,
            "entry_triggered": False,
            "diagnostics": {"reason": reason},
        }

    @staticmethod
    def _valid_bar(bar: CryptoBarLike) -> bool:
        return (
            getattr(bar, "high", None) is not None
            and getattr(bar, "low", None) is not None
            and getattr(bar, "close", None) is not None
        )

    @classmethod
    def _valid_contract_bar(cls, bar: CryptoBarLike) -> bool:
        return cls._valid_bar(bar) and getattr(bar, "open", None) is not None

    @staticmethod
    def _valid_perpetual_bar(bar: CryptoBarLike) -> bool:
        return all(
            getattr(bar, field, None) is not None
            for field in (
                "execution_open",
                "execution_high",
                "execution_low",
                "execution_close",
                "mark_open",
                "mark_high",
                "mark_low",
                "mark_close",
            )
        )

    @staticmethod
    def _execution_price(bar: CryptoBarLike, field: str) -> float:
        value = getattr(bar, f"execution_{field}", None)
        if value is None:
            value = getattr(bar, field, None)
        return float(value)

    @classmethod
    def _missed_move_metrics(
        cls,
        *,
        direction: str,
        reference_price: float,
        bars: Sequence[CryptoBarLike],
    ) -> dict[str, Optional[float]]:
        if reference_price <= 0 or not bars:
            return {
                "missed_favorable_move_pct": None,
                "missed_adverse_move_pct": None,
            }
        highest = max(cls._execution_price(bar, "high") for bar in bars)
        lowest = min(cls._execution_price(bar, "low") for bar in bars)
        if direction == "short":
            favorable = (reference_price - lowest) / reference_price * 100.0
            adverse = (highest - reference_price) / reference_price * 100.0
        else:
            favorable = (highest - reference_price) / reference_price * 100.0
            adverse = (reference_price - lowest) / reference_price * 100.0
        return {
            "missed_favorable_move_pct": round(max(favorable, 0.0), 4),
            "missed_adverse_move_pct": round(max(adverse, 0.0), 4),
        }

    @staticmethod
    def _mark_price(bar: CryptoBarLike, field: str) -> float:
        value = getattr(bar, f"mark_{field}", None)
        if value is None:
            value = getattr(bar, field, None)
        return float(value)

    @classmethod
    def _position_sizing(
        cls,
        *,
        entry_price: float,
        stop_loss: Optional[float],
        config: CryptoPlanBacktestConfig,
        position_multiplier_cap: float = 1.0,
    ) -> dict[str, Any]:
        try:
            multiplier = float(position_multiplier_cap)
        except (TypeError, ValueError):
            multiplier = 1.0
        multiplier = max(0.0, min(multiplier, 1.0))
        initial_equity = max(float(config.initial_equity), 0.0)
        risk_budget = (
            initial_equity
            * max(float(config.risk_per_trade_pct), 0.0)
            / 100.0
            * multiplier
        )
        max_notional = (
            initial_equity
            * max(float(config.max_notional_pct), 0.0)
            / 100.0
            * max(float(config.leverage), 0.0)
            * multiplier
        )
        if stop_loss is not None and stop_loss > 0 and stop_loss != entry_price:
            stop_distance = abs(entry_price - float(stop_loss))
            risk_sized_qty = risk_budget / stop_distance if stop_distance > 0 else 0.0
            sizing_method = "risk"
        else:
            risk_sized_qty = max_notional / entry_price if entry_price > 0 else 0.0
            sizing_method = "notional"
        max_qty = max_notional / entry_price if entry_price > 0 else 0.0
        quantity = max(0.0, min(risk_sized_qty, max_qty))
        return {
            "initial_equity": initial_equity,
            "risk_budget": risk_budget,
            "max_notional": max_notional,
            "quantity": quantity,
            "position_notional": quantity * entry_price,
            "sizing_method": sizing_method,
            "position_multiplier_cap": multiplier,
        }

    @classmethod
    def _evaluate_liquidation(
        cls,
        *,
        direction: str,
        entry_price: float,
        trade_bars: Sequence[CryptoBarLike],
        quantity: float,
        margin_mode: str,
        config: CryptoPlanBacktestConfig,
    ) -> Optional[tuple[datetime, int, float]]:
        leverage = max(float(config.leverage), 0.0)
        maintenance_rate = max(float(config.maintenance_margin_rate), 0.0)
        if leverage <= 1.0 or quantity <= 0 or maintenance_rate >= 1.0:
            return None

        if margin_mode == "cross":
            initial_equity = max(float(config.initial_equity), 0.0)
            if direction == "short":
                liquidation_price = (
                    initial_equity + quantity * entry_price
                ) / (quantity * (1 + maintenance_rate))
            else:
                numerator = quantity * entry_price - initial_equity
                if numerator <= 0:
                    return None
                liquidation_price = numerator / (quantity * (1 - maintenance_rate))
        elif direction == "short":
            liquidation_price = entry_price * (1 + 1 / leverage) / (1 + maintenance_rate)
        else:
            liquidation_price = entry_price * (1 - 1 / leverage) / (1 - maintenance_rate)

        for index, bar in enumerate(trade_bars, start=1):
            if direction == "short":
                liquidated = cls._mark_price(bar, "high") >= liquidation_price
            else:
                liquidated = cls._mark_price(bar, "low") <= liquidation_price
            if liquidated:
                return bar.timestamp, index, float(liquidation_price)
        return None

    @classmethod
    def _funding_cost(
        cls,
        *,
        direction: str,
        quantity: float,
        trade_bars: Sequence[CryptoBarLike],
    ) -> float:
        direction_sign = 1.0 if direction == "long" else -1.0
        total = 0.0
        for bar in trade_bars:
            rates = getattr(bar, "funding_rates", ()) or ()
            total += quantity * cls._mark_price(bar, "close") * sum(float(rate) for rate in rates) * direction_sign
        return total

    @staticmethod
    def _find_entry_index(bars: Sequence[CryptoBarLike], entry_price: float) -> Optional[int]:
        for index, bar in enumerate(bars):
            low = float(bar.low)
            high = float(bar.high)
            if low <= entry_price <= high:
                return index
        return None

    @classmethod
    def _evaluate_targets(
        cls,
        *,
        direction: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        trade_bars: Sequence[CryptoBarLike],
        end_close: float,
    ) -> tuple[Optional[bool], Optional[bool], str, Optional[datetime], Optional[int], float, str]:
        hit_sl: Optional[bool] = None if stop_loss is None else False
        hit_tp: Optional[bool] = None if take_profit is None else False
        first_hit = "neither"
        first_hit_at: Optional[datetime] = None
        first_hit_bars: Optional[int] = None
        exit_price = float(end_close)
        exit_reason = "window_end"

        if stop_loss is None and take_profit is None:
            return hit_sl, hit_tp, first_hit, first_hit_at, first_hit_bars, exit_price, exit_reason

        for index, bar in enumerate(trade_bars, start=1):
            low = float(bar.low)
            high = float(bar.high)
            if direction == "short":
                stop_hit = stop_loss is not None and high >= stop_loss
                take_hit = take_profit is not None and low <= take_profit
            else:
                stop_hit = stop_loss is not None and low <= stop_loss
                take_hit = take_profit is not None and high >= take_profit

            if stop_hit:
                hit_sl = True
            if take_hit:
                hit_tp = True
            if not stop_hit and not take_hit:
                continue

            first_hit_at = bar.timestamp
            first_hit_bars = index
            if stop_hit and take_hit:
                return hit_sl, hit_tp, "ambiguous", first_hit_at, first_hit_bars, float(stop_loss), "ambiguous_stop_loss"
            if stop_hit:
                return hit_sl, hit_tp, "stop_loss", first_hit_at, first_hit_bars, float(stop_loss), "stop_loss"
            return hit_sl, hit_tp, "take_profit", first_hit_at, first_hit_bars, float(take_profit), "take_profit"

        return hit_sl, hit_tp, first_hit, first_hit_at, first_hit_bars, exit_price, exit_reason

    @staticmethod
    def _directional_return_pct(*, direction: str, entry_price: float, exit_price: float) -> float:
        if direction == "short":
            return round((entry_price - exit_price) / entry_price * 100, 4)
        return round((exit_price - entry_price) / entry_price * 100, 4)

    @classmethod
    def _simulate_trade(
        cls,
        *,
        direction: str,
        entry_price: float,
        exit_price: float,
        stop_loss: Optional[float],
        config: CryptoPlanBacktestConfig,
        sizing: Optional[dict[str, Any]] = None,
        funding_cost: float = 0.0,
        entry_order_type: str = "market",
        exit_order_type: str = "market",
        position_multiplier_cap: float = 1.0,
    ) -> dict[str, Any]:
        sizing = sizing or cls._position_sizing(
            entry_price=entry_price,
            stop_loss=stop_loss,
            config=config,
            position_multiplier_cap=position_multiplier_cap,
        )
        initial_equity = float(sizing["initial_equity"])
        risk_budget = float(sizing["risk_budget"])
        quantity = float(sizing["quantity"])
        notional = float(sizing["position_notional"])
        sizing_method = str(sizing["sizing_method"])
        applied_position_multiplier = float(sizing.get("position_multiplier_cap", 1.0))

        slippage_rate = max(float(config.slippage_bps), 0.0) / 10000.0
        uses_order_type_fees = str(config.engine_version).strip().lower() in {"btc-plan-v4", "btc-plan-v5"}
        if uses_order_type_fees:
            maker_rate = max(float(config.maker_fee_rate_bps), 0.0) / 10000.0
            taker_rate = max(float(config.taker_fee_rate_bps), 0.0) / 10000.0
            entry_fee_rate = maker_rate if entry_order_type == "limit" else taker_rate
            exit_fee_rate = maker_rate if exit_order_type == "limit" else taker_rate
        else:
            entry_fee_rate = exit_fee_rate = max(float(config.fee_rate_bps), 0.0) / 10000.0
        if direction == "short":
            executed_entry_price = entry_price * (1 - slippage_rate)
            executed_exit_price = exit_price * (1 + slippage_rate)
            gross_pnl = (executed_entry_price - executed_exit_price) * quantity
        else:
            executed_entry_price = entry_price * (1 + slippage_rate)
            executed_exit_price = exit_price * (1 - slippage_rate)
            gross_pnl = (executed_exit_price - executed_entry_price) * quantity

        entry_fee = abs(executed_entry_price * quantity) * entry_fee_rate
        exit_fee = abs(executed_exit_price * quantity) * exit_fee_rate
        total_fee = entry_fee + exit_fee
        net_pnl = gross_pnl - total_fee - float(funding_cost)
        r_multiple = round(net_pnl / risk_budget, 4) if risk_budget > 0 else None
        return {
            "initial_equity": round(initial_equity, 4),
            "risk_budget": round(risk_budget, 4),
            "position_notional": round(notional, 4),
            "quantity": round(quantity, 8),
            "sizing_method": sizing_method,
            "position_multiplier_cap": round(applied_position_multiplier, 4),
            "executed_entry_price": round(executed_entry_price, 4),
            "executed_exit_price": round(executed_exit_price, 4),
            "gross_pnl": round(gross_pnl, 4),
            "entry_fee": round(entry_fee, 4),
            "exit_fee": round(exit_fee, 4),
            "total_fee": round(total_fee, 4),
            "funding_cost": round(float(funding_cost), 4),
            "net_pnl": round(net_pnl, 4),
            "r_multiple": r_multiple,
            "net_return_pct": round(net_pnl / initial_equity * 100, 4) if initial_equity else 0.0,
            "gross_trade_return_pct": cls._directional_return_pct(
                direction=direction,
                entry_price=entry_price,
                exit_price=exit_price,
            ),
        }

    @staticmethod
    def _execution_config(config: CryptoPlanBacktestConfig) -> dict[str, Any]:
        return {
            "initial_equity": float(config.initial_equity),
            "risk_per_trade_pct": float(config.risk_per_trade_pct),
            "max_notional_pct": float(config.max_notional_pct),
            "leverage": float(config.leverage),
            "fee_rate_bps": float(config.fee_rate_bps),
            "slippage_bps": float(config.slippage_bps),
            "maker_fee_rate_bps": float(config.maker_fee_rate_bps),
            "taker_fee_rate_bps": float(config.taker_fee_rate_bps),
            "maintenance_margin_rate": float(config.maintenance_margin_rate),
        }

    @staticmethod
    def _classify_return(
        *,
        simulated_return_pct: Optional[float],
        neutral_band_pct: float,
    ) -> tuple[Optional[str], Optional[bool]]:
        if simulated_return_pct is None:
            return None, None
        band = abs(float(neutral_band_pct))
        value = float(simulated_return_pct)
        if value >= band:
            return "win", True
        if value <= -band:
            return "loss", False
        return "neutral", None

    @staticmethod
    def _max_value(values: Iterable[Optional[float]]) -> Optional[float]:
        items = [float(value) for value in values if value is not None]
        return max(items) if items else None

    @staticmethod
    def _min_value(values: Iterable[Optional[float]]) -> Optional[float]:
        items = [float(value) for value in values if value is not None]
        return min(items) if items else None

    @staticmethod
    def _average(values: Iterable[Optional[float]]) -> Optional[float]:
        items = [float(value) for value in values if value is not None]
        if not items:
            return None
        return round(sum(items) / len(items), 4)

    @staticmethod
    def _diagnostics(row: Any) -> dict[str, Any]:
        value = getattr(row, "diagnostics_json", None)
        if isinstance(value, str):
            try:
                import json

                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _diagnostic_number(cls, row: Any, *path: str) -> Optional[float]:
        current: Any = cls._diagnostics(row)
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        try:
            parsed = float(current)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed else None

    @classmethod
    def _summary_initial_equity(cls, rows: Sequence[Any]) -> float:
        for row in rows:
            value = cls._diagnostic_number(row, "trade", "initial_equity")
            if value is not None:
                return value
        return 0.0

    @classmethod
    def _equity_curve(cls, rows: Sequence[Any]) -> list[dict[str, Any]]:
        equity = cls._summary_initial_equity(rows)
        if equity <= 0:
            return []

        curve: list[dict[str, Any]] = []
        ordered = sorted(
            rows,
            key=lambda row: (
                getattr(row, "analysis_created_at", None) or datetime.min,
                getattr(row, "plan_type", "") or "",
            ),
        )
        for index, row in enumerate(ordered, start=1):
            pnl = cls._diagnostic_number(row, "trade", "net_pnl")
            if pnl is None:
                continue
            equity += pnl
            timestamp = getattr(row, "analysis_created_at", None)
            curve.append(
                {
                    "index": index,
                    "analysis_history_id": getattr(row, "analysis_history_id", None),
                    "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else None,
                    "equity": round(equity, 4),
                    "net_pnl": round(pnl, 4),
                }
            )
        return curve

    @classmethod
    def _portfolio_risk_metrics(
        cls,
        *,
        returns: Sequence[float],
        r_multiples: Sequence[float],
        pnl_values: Sequence[float],
        equity_curve: Sequence[dict[str, Any]],
        initial_equity: float,
        total_fees: float,
        total_funding_cost: float,
    ) -> dict[str, Any]:
        final_equity = float(equity_curve[-1]["equity"]) if equity_curve else initial_equity
        total_return_pct = (
            round((final_equity - initial_equity) / initial_equity * 100, 4)
            if initial_equity
            else None
        )
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "initial_equity": round(initial_equity, 4),
            "final_equity": round(final_equity, 4) if final_equity else None,
            "total_return_pct": total_return_pct,
            "total_net_pnl": round(sum(pnl_values), 4) if pnl_values else 0.0,
            "total_fees": round(total_fees, 4),
            "total_funding_cost": round(total_funding_cost, 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
            "avg_trade_net_pnl": cls._average(pnl_values),
            "best_trade_return_pct": round(max(returns), 4) if returns else None,
            "worst_trade_return_pct": round(min(returns), 4) if returns else None,
            "avg_r_multiple": cls._average(r_multiples),
            "best_r_multiple": round(max(r_multiples), 4) if r_multiples else None,
            "worst_r_multiple": round(min(r_multiples), 4) if r_multiples else None,
            "max_drawdown_pct": cls._max_drawdown_pct(equity_curve),
            "expectancy_pct": cls._average(returns),
        }

    @staticmethod
    def _sample_confidence(*, sample_count: int, minimum_sample_count: int) -> dict[str, Any]:
        return {
            "sample_count": int(sample_count),
            "minimum_sample_count": int(minimum_sample_count),
            "level": "low" if int(sample_count) < int(minimum_sample_count) else "normal",
            "is_low_confidence": int(sample_count) < int(minimum_sample_count),
            "reason": "sample_count_below_minimum" if int(sample_count) < int(minimum_sample_count) else None,
        }

    @staticmethod
    def _max_drawdown_pct(equity_curve: Sequence[dict[str, Any]]) -> Optional[float]:
        peak: Optional[float] = None
        max_drawdown = 0.0
        for point in equity_curve:
            try:
                equity = float(point.get("equity"))
            except (TypeError, ValueError):
                continue
            peak = equity if peak is None else max(peak, equity)
            if peak and peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        return round(max_drawdown, 4) if equity_curve else None
