#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the offline BTC multi-task baseline on an OHLCV CSV."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.services.btc_training import (  # noqa: E402
    BtcTrainingConfig,
    BtcTrainingService,
    DEFAULT_COST_SENSITIVITY_BPS,
    DEFAULT_HOLDOUT_BARS,
    SUPPORTED_MODELS,
)

DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")


def _parse_horizons(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in str(value or "").split(","):
        name, separator, bars = item.partition(":")
        if not separator or not name.strip():
            raise argparse.ArgumentTypeError("horizons 应为 name:bars,name:bars，例如 15m:3,1h:12,4h:48")
        try:
            result[name.strip()] = int(bars)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效 horizon bars: {bars}") from exc
    return result


def _parse_cost_sensitivity(value: str) -> tuple[float, ...]:
    try:
        result = tuple(float(item.strip()) for item in str(value or "").split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("cost-sensitivity-bps 应为逗号分隔的非负数字，例如 14,30,50") from exc
    if not result or any(item < 0 for item in result):
        raise argparse.ArgumentTypeError("cost-sensitivity-bps 应包含至少一个非负数字")
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线运行 BTC 收益分布、波动和市场状态基线。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="OHLCV CSV 路径。")
    parser.add_argument("--output", type=Path, help="可选 JSON 输出路径；不指定则打印到 stdout。")
    parser.add_argument("--horizons", type=_parse_horizons, default=dict(BtcTrainingConfig().horizons), help="horizon 配置，例如 15m:3,1h:12,4h:48。")
    parser.add_argument("--lookback-bars", type=int, default=72)
    parser.add_argument("--min-train-bars", type=int, default=336)
    parser.add_argument("--validation-bars", type=int, default=48)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--neutral-band", type=float, default=0.002, help="方向标签中性带，默认 0.2%%（小数 0.002）。")
    parser.add_argument("--bar-hours", type=float, default=1.0, help="CSV 每根 K 线覆盖小时数，默认 1。")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default="linear", help="模型族；默认 linear，可选 lightgbm。")
    parser.add_argument("--random-state", type=int, default=42, help="LightGBM 随机种子。")
    parser.add_argument("--fee-bps-per-side", type=float, default=5.0, help="执行评估每边手续费（bps）。")
    parser.add_argument("--slippage-bps-per-side", type=float, default=2.0, help="执行评估每边滑点（bps）。")
    parser.add_argument("--min-trade-edge-bps", type=float, default=0.0, help="预测收益额外需要超过的最小边际（bps）。")
    parser.add_argument(
        "--decision-stride",
        type=int,
        default=None,
        help="执行评估决策间隔（bar）；默认使用 horizon，避免重叠交易。",
    )
    parser.add_argument(
        "--holdout-bars",
        type=int,
        default=DEFAULT_HOLDOUT_BARS,
        help="固定末段 holdout 的 bar 数；默认 168，设为 0 可关闭。",
    )
    parser.add_argument(
        "--cost-sensitivity-bps",
        type=_parse_cost_sensitivity,
        default=DEFAULT_COST_SENSITIVITY_BPS,
        help="holdout 成本敏感性评估的双边成本场景，例如 14,30,50。",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        bars = pd.read_csv(args.input)
        config = BtcTrainingConfig(
            horizons=args.horizons,
            lookback_bars=args.lookback_bars,
            min_train_bars=args.min_train_bars,
            validation_bars=args.validation_bars,
            folds=args.folds,
            neutral_band=args.neutral_band,
            model=args.model,
            random_state=args.random_state,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            min_trade_edge_bps=args.min_trade_edge_bps,
            decision_stride=args.decision_stride,
            holdout_bars=args.holdout_bars,
            cost_sensitivity_bps=args.cost_sensitivity_bps,
        )
        result = BtcTrainingService(config).build(bars, bar_hours=args.bar_hours)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"训练失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
