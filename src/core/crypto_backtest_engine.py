# -*- coding: utf-8 -*-
"""BTC plan-level backtesting engine.

This module is deliberately DB-agnostic. It evaluates one extracted BTC
strategy plan against forward OHLC bars and returns serializable metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Protocol, Sequence


class CryptoBarLike(Protocol):
    """Protocol for OHLC bars used by BTC plan backtests."""

    timestamp: datetime
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]


@dataclass(frozen=True)
class CryptoPlan:
    plan_type: str
    horizon: str
    direction: str
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    raw_plan: dict[str, Any]


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


class CryptoBacktestEngine:
    """Evaluate long/short BTC plans using forward candles."""

    @classmethod
    def evaluate_plan(
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
                "entry_triggered": False,
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
        )
        simulated_return_pct = trade["net_return_pct"]
        outcome, direction_correct = cls._classify_return(
            simulated_return_pct=simulated_return_pct,
            neutral_band_pct=config.neutral_band_pct,
        )

        return {
            **cls._base(plan, eval_status="completed"),
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
        triggered = [row for row in completed if getattr(row, "entry_triggered", None) is True]
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
                {"total": 0, "completed": 0, "triggered": 0, "wins": 0, "losses": 0},
            )
            bucket["total"] += 1
            if getattr(row, "eval_status", None) == "completed":
                bucket["completed"] += 1
            if getattr(row, "entry_triggered", None) is True:
                bucket["triggered"] += 1
            if getattr(row, "outcome", None) == "win":
                bucket["wins"] += 1
            if getattr(row, "outcome", None) == "loss":
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
        equity_curve = cls._equity_curve(triggered)
        risk_metrics = cls._portfolio_risk_metrics(
            returns=[value for value in returns if value is not None],
            r_multiples=[value for value in r_multiples if value is not None],
            pnl_values=[value for value in pnl_values if value is not None],
            equity_curve=equity_curve,
            initial_equity=cls._summary_initial_equity(triggered),
            total_fees=total_fees,
        )
        sample_confidence = cls._sample_confidence(
            sample_count=len(triggered),
            minimum_sample_count=30,
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
            },
        }

    @staticmethod
    def _base(plan: CryptoPlan, *, eval_status: str) -> dict[str, Any]:
        return {
            "plan_type": plan.plan_type,
            "horizon": plan.horizon,
            "direction": plan.direction,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "eval_status": eval_status,
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
    ) -> dict[str, Any]:
        initial_equity = max(float(config.initial_equity), 0.0)
        risk_budget = initial_equity * max(float(config.risk_per_trade_pct), 0.0) / 100.0
        max_notional = initial_equity * max(float(config.max_notional_pct), 0.0) / 100.0 * max(float(config.leverage), 0.0)

        if stop_loss is not None and stop_loss > 0 and stop_loss != entry_price:
            stop_distance = abs(entry_price - float(stop_loss))
            risk_sized_qty = risk_budget / stop_distance if stop_distance > 0 else 0.0
            sizing_method = "risk"
        else:
            risk_sized_qty = max_notional / entry_price if entry_price > 0 else 0.0
            sizing_method = "notional"

        max_qty = max_notional / entry_price if entry_price > 0 else 0.0
        quantity = max(0.0, min(risk_sized_qty, max_qty))
        notional = quantity * entry_price

        slippage_rate = max(float(config.slippage_bps), 0.0) / 10000.0
        fee_rate = max(float(config.fee_rate_bps), 0.0) / 10000.0
        if direction == "short":
            executed_entry_price = entry_price * (1 - slippage_rate)
            executed_exit_price = exit_price * (1 + slippage_rate)
            gross_pnl = (executed_entry_price - executed_exit_price) * quantity
        else:
            executed_entry_price = entry_price * (1 + slippage_rate)
            executed_exit_price = exit_price * (1 - slippage_rate)
            gross_pnl = (executed_exit_price - executed_entry_price) * quantity

        entry_fee = abs(executed_entry_price * quantity) * fee_rate
        exit_fee = abs(executed_exit_price * quantity) * fee_rate
        total_fee = entry_fee + exit_fee
        net_pnl = gross_pnl - total_fee
        r_multiple = round(net_pnl / risk_budget, 4) if risk_budget > 0 else None
        return {
            "initial_equity": round(initial_equity, 4),
            "risk_budget": round(risk_budget, 4),
            "position_notional": round(notional, 4),
            "quantity": round(quantity, 8),
            "sizing_method": sizing_method,
            "executed_entry_price": round(executed_entry_price, 4),
            "executed_exit_price": round(executed_exit_price, 4),
            "gross_pnl": round(gross_pnl, 4),
            "entry_fee": round(entry_fee, 4),
            "exit_fee": round(exit_fee, 4),
            "total_fee": round(total_fee, 4),
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
