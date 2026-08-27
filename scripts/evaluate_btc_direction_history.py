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
from typing import Any, Optional, Sequence

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


def _accuracy(correct: list[bool]) -> Optional[float]:
    return round(sum(correct) / len(correct), 6) if correct else None


def evaluate_history(
    bars: pd.DataFrame,
    *,
    horizon_bars: int = 24,
    lookback_bars: int = 60,
    step: int = 24,
    random_seed: int = 7,
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
    rng = random.Random(int(random_seed))
    model_correct: list[bool] = []
    always_long: list[bool] = []
    previous_direction: list[bool] = []
    random_direction: list[bool] = []
    signal_counts = {"long": 0, "short": 0, "neutral": 0}
    evaluated = 0

    for index in range(lookback - 1, len(frame) - horizon, stride):
        window = frame.iloc[max(0, index - lookback + 1) : index + 1].copy()
        window.attrs["period"] = "hourly"
        window.attrs["fetched_at"] = frame.loc[index, "date"] + pd.Timedelta(hours=1)
        context = build_crypto_technical_context(window, "BTC", lookback=lookback)
        direction = _infer_bias(context)
        signal_counts[direction] = signal_counts.get(direction, 0) + 1
        future = frame.iloc[index + 1 : index + 1 + horizon]
        close = float(frame.loc[index, "close"])
        model_result = _direction_score(direction, close, future)
        if model_result is not None:
            model_correct.append(model_result)
            evaluated += 1

        long_result = _direction_score("long", close, future)
        if long_result is not None:
            always_long.append(long_result)

        if index > 0:
            prior_direction = "long" if float(frame.loc[index, "close"]) >= float(frame.loc[index - 1, "close"]) else "short"
            prior_result = _direction_score(prior_direction, close, future)
            if prior_result is not None:
                previous_direction.append(prior_result)

        random_direction_name = "long" if rng.random() >= 0.5 else "short"
        random_result = _direction_score(random_direction_name, close, future)
        if random_result is not None:
            random_direction.append(random_result)

    return {
        "input_rows": int(len(frame)),
        "horizon_bars": horizon,
        "lookback_bars": lookback,
        "step": stride,
        "mfe_mae_definition": "direction_correct = mfe_pct > mae_pct",
        "deterministic_vote": {
            "signals": signal_counts,
            "evaluated_signals": evaluated,
            "directional_accuracy": _accuracy(model_correct),
        },
        "baselines": {
            "always_long": {"samples": len(always_long), "directional_accuracy": _accuracy(always_long)},
            "previous_bar_direction": {
                "samples": len(previous_direction),
                "directional_accuracy": _accuracy(previous_direction),
            },
            "random_50_50": {
                "seed": int(random_seed),
                "samples": len(random_direction),
                "directional_accuracy": _accuracy(random_direction),
            },
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线评估 BTC 确定性方向投票与朴素基线。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="1h 训练 CSV 路径。")
    parser.add_argument("--horizon-bars", type=int, default=24, help="MFE/MAE 前瞻窗口，默认 24 根。")
    parser.add_argument("--lookback-bars", type=int, default=60, help="指标上下文窗口，默认 60 根。")
    parser.add_argument("--step", type=int, default=24, help="抽样步长，默认每 24 根 K 线评估；传 1 可全量逐根评估。")
    parser.add_argument("--seed", type=int, default=7, help="随机基线种子。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = evaluate_history(
            pd.read_csv(args.input),
            horizon_bars=args.horizon_bars,
            lookback_bars=args.lookback_bars,
            step=args.step,
            random_seed=args.seed,
        )
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"评估失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
