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

from src.services.btc_training import BtcTrainingConfig, BtcTrainingService  # noqa: E402

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
