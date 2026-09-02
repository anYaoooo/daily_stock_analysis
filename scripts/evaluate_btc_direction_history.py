#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate the deterministic BTC direction vote on historical 1h candles.

This is an offline research tool. It reads the CSV produced by
``backfill_btc_history.py`` and never writes to the application database.
Signals are scored with the same MFE/MAE definition used by the BTC backtest.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.crypto_technical import _infer_bias, build_crypto_technical_context  # noqa: E402

DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")


def _direction_score(direction: str, close: float, future: pd.DataFrame) -> Optional[bool]:
    if direction not in {"long", "short"} or close <= 0 or future.empty:
        return None
    high = float(future["high"].max())
    low = float(future["low"].min())
    if direction == "long":
        mfe = max((high - close) / close * 100.0, 0.0)
        mae = max((close - low) / close * 100.0, 0.0)
    else:
        mfe = max((close - low) / close * 100.0, 0.0)
        mae = max((high - close) / close * 100.0, 0.0)
    return bool(mfe > mae)


def _close_direction_score(direction: str, entry_price: float, exit_price: float) -> Optional[bool]:
    """Whether the realized close-to-close trade return had the right sign.

    ``MFE > MAE`` is retained as a setup/opportunity metric, but it can mark a
    trade correct even when the window ends in a loss. This metric is the
    stricter outcome definition used alongside it.
    """
    signed = _signed_return_pct(direction, entry_price, exit_price)
    return signed > 0 if signed is not None else None


def _accuracy(correct: list[bool]) -> Optional[float]:
    return round(sum(correct) / len(correct), 6) if correct else None


def _signed_return_pct(direction: str, entry_price: float, exit_price: float) -> Optional[float]:
    """Return the gross directional return for one long/short round trip."""
    if direction not in {"long", "short"} or entry_price <= 0 or exit_price <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    return sign * (exit_price / entry_price - 1.0) * 100.0


def _net_return_pct(
    direction: str,
    entry_price: float,
    exit_price: float,
    *,
    fee_bps: float,
    slippage_bps: float,
) -> Optional[float]:
    """Apply a transparent round-trip fee/slippage approximation.

    The historical script has no order-book data, so slippage is modeled as a
    fixed cost on both sides. This is deliberately conservative and is not a
    substitute for the execution engine's fill simulation.
    """
    gross = _signed_return_pct(direction, entry_price, exit_price)
    if gross is None:
        return None
    round_trip_cost_pct = 2.0 * max(float(fee_bps), 0.0) / 100.0
    round_trip_cost_pct += 2.0 * max(float(slippage_bps), 0.0) / 100.0
    return gross - round_trip_cost_pct


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 6) if values else None


def _trade_metrics(
    records: list[Dict[str, Any]],
    *,
    fee_bps: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    correct = [bool(item["correct"]) for item in records]
    close_correct = [bool(item["close_correct"]) for item in records]
    gross = [float(item["gross_return_pct"]) for item in records]
    net = [float(item["net_return_pct"]) for item in records]
    equity = 1.0
    peak = 1.0
    max_drawdown_pct = 0.0
    for value in net:
        equity *= max(0.0, 1.0 + value / 100.0)
        peak = max(peak, equity)
        max_drawdown_pct = min(max_drawdown_pct, (equity / peak - 1.0) * 100.0)
    gross_profit = sum(value for value in net if value > 0)
    gross_loss = abs(sum(value for value in net if value < 0))
    return {
        "samples": len(records),
        "directional_accuracy": _accuracy(correct),
        "mfe_mae_accuracy": _accuracy(correct),
        "close_directional_accuracy": _accuracy(close_correct),
        "avg_gross_return_pct": _mean(gross),
        "avg_net_return_pct": _mean(net),
        "net_positive_rate": round(sum(value > 0 for value in net) / len(net), 6) if net else None,
        "cumulative_net_return_pct": round((equity - 1.0) * 100.0, 6) if records else None,
        "max_drawdown_pct": round(max_drawdown_pct, 6) if records else None,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "round_trip_cost_bps": round(2.0 * (max(float(fee_bps), 0.0) + max(float(slippage_bps), 0.0)), 4),
    }


def _direction_from_score(score: Any, threshold: float) -> Optional[str]:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if value >= threshold:
        return "long"
    if value <= -threshold:
        return "short"
    return "neutral"


def _evaluate_single_horizon(
    frame: pd.DataFrame,
    *,
    horizon: int,
    lookback: int,
    stride: int,
    random_seed: int,
    fee_bps: float,
    slippage_bps: float,
    direction_threshold: float,
) -> dict[str, Any]:
    rng = random.Random(int(random_seed))
    model_correct: list[bool] = []
    always_long: list[bool] = []
    previous_direction: list[bool] = []
    random_direction: list[bool] = []
    always_long_close: list[bool] = []
    previous_direction_close: list[bool] = []
    random_direction_close: list[bool] = []
    model_records: list[Dict[str, Any]] = []
    model_records_by_direction: Dict[str, list[Dict[str, Any]]] = {"long": [], "short": []}
    signal_counts = {"long": 0, "short": 0, "neutral": 0}
    candidate_count = 0

    for index in range(lookback - 1, len(frame) - horizon, stride):
        candidate_count += 1
        window = frame.iloc[max(0, index - lookback + 1) : index + 1].copy()
        window.attrs["period"] = "hourly"
        window.attrs["fetched_at"] = frame.loc[index, "date"] + pd.Timedelta(hours=1)
        context = build_crypto_technical_context(window, "BTC", lookback=lookback)
        score = None
        if isinstance(context, dict) and isinstance(context.get("direction"), dict):
            score = context["direction"].get("score")
        direction = _direction_from_score(score, direction_threshold) or _infer_bias(context)
        signal_counts[direction] = signal_counts.get(direction, 0) + 1
        future = frame.iloc[index + 1 : index + 1 + horizon]
        close = float(frame.loc[index, "close"])
        model_result = _direction_score(direction, close, future)
        if model_result is not None:
            model_correct.append(model_result)

            entry_price = float(future.iloc[0]["open"])
            exit_price = float(future.iloc[-1]["close"])
            gross = _signed_return_pct(direction, entry_price, exit_price)
            close_correct = _close_direction_score(direction, entry_price, exit_price)
            net = _net_return_pct(
                direction,
                entry_price,
                exit_price,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            if gross is not None and net is not None:
                record = {
                    "direction": direction,
                    "correct": model_result,
                    "close_correct": bool(close_correct),
                    "gross_return_pct": gross,
                    "net_return_pct": net,
                    "score": float(score) if score is not None else None,
                }
                model_records.append(record)
                model_records_by_direction[direction].append(record)

        long_result = _direction_score("long", close, future)
        if long_result is not None:
            always_long.append(long_result)
            always_long_close.append(bool(_close_direction_score("long", float(future.iloc[0]["open"]), float(future.iloc[-1]["close"]))))

        if index > 0:
            prior_direction = "long" if float(frame.loc[index, "close"]) >= float(frame.loc[index - 1, "close"]) else "short"
            prior_result = _direction_score(prior_direction, close, future)
            if prior_result is not None:
                previous_direction.append(prior_result)
                previous_direction_close.append(bool(_close_direction_score(prior_direction, float(future.iloc[0]["open"]), float(future.iloc[-1]["close"]))))

        random_direction_name = "long" if rng.random() >= 0.5 else "short"
        random_result = _direction_score(random_direction_name, close, future)
        if random_result is not None:
            random_direction.append(random_result)
            random_direction_close.append(bool(_close_direction_score(random_direction_name, float(future.iloc[0]["open"]), float(future.iloc[-1]["close"]))))

    model_metrics = _trade_metrics(model_records, fee_bps=fee_bps, slippage_bps=slippage_bps)
    model_metrics["signals"] = signal_counts
    model_metrics["evaluated_signals"] = len(model_correct)
    model_metrics["candidate_bars"] = candidate_count
    model_metrics["direction_threshold"] = round(float(direction_threshold), 6)
    model_metrics["coverage"] = round(len(model_correct) / candidate_count, 6) if candidate_count else None
    model_metrics["by_direction"] = {
        direction: _trade_metrics(records, fee_bps=fee_bps, slippage_bps=slippage_bps)
        for direction, records in model_records_by_direction.items()
    }
    strength_buckets = {
        "threshold_to_0.60": [],
        "0.60_to_0.75": [],
        "0.75_to_1.00": [],
    }
    for record in model_records:
        score_value = abs(float(record.get("score") or 0.0))
        if score_value < 0.60:
            strength_buckets["threshold_to_0.60"].append(record)
        elif score_value < 0.75:
            strength_buckets["0.60_to_0.75"].append(record)
        else:
            strength_buckets["0.75_to_1.00"].append(record)
    model_metrics["by_strength"] = {
        name: _trade_metrics(records, fee_bps=fee_bps, slippage_bps=slippage_bps)
        for name, records in strength_buckets.items()
    }

    return {
        "horizon_bars": horizon,
        "direction_threshold": round(float(direction_threshold), 6),
        "mfe_mae_definition": "direction_correct = mfe_pct > mae_pct (opportunity metric)",
        "close_direction_definition": "close_direction_correct = signed_return(entry=next_open, exit=window_close) > 0",
        "trade_return_definition": "entry=next_bar_open; exit=last_horizon_bar_close; net=gross-round_trip_fee_and_slippage",
        "trading_costs": {
            "fee_bps_per_side": max(float(fee_bps), 0.0),
            "slippage_bps_per_side": max(float(slippage_bps), 0.0),
            "round_trip_cost_bps": round(2.0 * (max(float(fee_bps), 0.0) + max(float(slippage_bps), 0.0)), 4),
        },
        "deterministic_vote": {
            **model_metrics,
            "directional_accuracy": _accuracy(model_correct),
        },
        "baselines": {
            "always_long": {
                "samples": len(always_long),
                "directional_accuracy": _accuracy(always_long),
                "close_directional_accuracy": _accuracy(always_long_close),
            },
            "previous_bar_direction": {
                "samples": len(previous_direction),
                "directional_accuracy": _accuracy(previous_direction),
                "close_directional_accuracy": _accuracy(previous_direction_close),
            },
            "random_50_50": {
                "seed": int(random_seed),
                "samples": len(random_direction),
                "directional_accuracy": _accuracy(random_direction),
                "close_directional_accuracy": _accuracy(random_direction_close),
            },
        },
    }


def evaluate_history(
    bars: pd.DataFrame,
    *,
    horizon_bars: int = 24,
    lookback_bars: int = 60,
    step: int = 24,
    random_seed: int = 7,
    horizons: Optional[Sequence[int]] = None,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    direction_threshold: float = 0.45,
) -> dict[str, Any]:
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(required)).sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[frame["close"] > 0].reset_index(drop=True)
    horizon = max(1, int(horizon_bars))
    lookback = max(20, int(lookback_bars))
    stride = max(1, int(step))
    threshold = min(max(float(direction_threshold), 0.0), 1.0)
    horizon_values = [horizon]
    if horizons is not None:
        horizon_values.extend(max(1, int(value)) for value in horizons)
        horizon_values = list(dict.fromkeys(horizon_values))

    evaluations = {
        str(value): _evaluate_single_horizon(
            frame,
            horizon=value,
            lookback=lookback,
            stride=stride,
            random_seed=random_seed,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            direction_threshold=threshold,
        )
        for value in horizon_values
    }
    primary = evaluations.get(str(horizon)) or next(iter(evaluations.values()))
    result = {
        "input_rows": int(len(frame)),
        "horizon_bars": horizon,
        "lookback_bars": lookback,
        "step": stride,
        "direction_threshold": threshold,
        **primary,
    }
    if horizons is not None:
        result["horizon_evaluations"] = evaluations
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线评估 BTC 确定性方向投票与朴素基线。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="1h 训练 CSV 路径。")
    parser.add_argument("--horizon-bars", type=int, default=24, help="MFE/MAE 前瞻窗口，默认 24 根。")
    parser.add_argument(
        "--horizons",
        type=str,
        default=None,
        help="逗号分隔的多个前瞻窗口，例如 1,4,12,24；传入后同时输出各窗口统计。",
    )
    parser.add_argument("--lookback-bars", type=int, default=60, help="指标上下文窗口，默认 60 根。")
    parser.add_argument("--step", type=int, default=24, help="抽样步长，默认每 24 根 K 线评估；传 1 可全量逐根评估。")
    parser.add_argument("--seed", type=int, default=7, help="随机基线种子。")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费（bps），默认 5。")
    parser.add_argument("--slippage-bps", type=float, default=2.0, help="每边滑点（bps），默认 2。")
    parser.add_argument("--direction-threshold", type=float, default=0.45, help="对称方向分数触发多空的绝对阈值，默认 0.45。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        horizons = None
        if args.horizons:
            horizons = [int(item.strip()) for item in args.horizons.split(",") if item.strip()]
        result = evaluate_history(
            pd.read_csv(args.input),
            horizon_bars=args.horizon_bars,
            lookback_bars=args.lookback_bars,
            step=args.step,
            random_seed=args.seed,
            horizons=horizons,
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
            direction_threshold=args.direction_threshold,
        )
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"评估失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
