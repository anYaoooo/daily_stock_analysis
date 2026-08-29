#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the offline BTC PatchTST/iTransformer research trainer on a CSV."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.services.btc_transformer import (  # noqa: E402
    TransformerFeatureConfig,
    TransformerTrainingConfig,
    WalkForwardTransformerTrainer,
)


DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")
DEFAULT_HOURLY_HORIZONS = {"1h": 1, "4h": 4, "24h": 24}


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
    parser = argparse.ArgumentParser(description="离线训练 BTC PatchTST/iTransformer 多任务研究模型。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="OHLCV CSV 路径。")
    parser.add_argument("--output", type=Path, help="可选 JSON 输出路径。")
    parser.add_argument("--architecture", choices=("patchtst", "itransformer", "fusion"), default="patchtst")
    parser.add_argument("--horizons", type=_parse_horizons, default=dict(DEFAULT_HOURLY_HORIZONS), help="例如小时线 1h:1,4h:4,24h:24；5 分钟线可用 15m:3,1h:12,4h:48。")
    parser.add_argument("--sequence-length", type=int, default=256)
    parser.add_argument("--patch-length", type=int, default=16)
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--d-model", type=int, default=128)
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--layers", type=int, default=3)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--min-train-samples", type=int, default=336)
    parser.add_argument("--validation-samples", type=int, default=48)
    parser.add_argument("--purge-samples", type=int, default=48)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--bar-hours", type=float, default=1.0, help="每根 K 线覆盖小时数，5m 数据为 0.0833333。")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--device", default="cpu", help="PyTorch 设备，例如 cpu、cuda 或 cuda:0；默认 cpu。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        bars = pd.read_csv(args.input)
        feature_config = TransformerFeatureConfig(
            horizons=args.horizons,
            sequence_length=args.sequence_length,
            bar_hours=args.bar_hours,
        )
        config = TransformerTrainingConfig(
            feature=feature_config,
            architecture=args.architecture,
            patch_length=args.patch_length,
            stride=args.stride,
            d_model=args.d_model,
            n_heads=args.heads,
            layers=args.layers,
            epochs=args.epochs,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            min_train_samples=args.min_train_samples,
            validation_samples=args.validation_samples,
            purge_samples=args.purge_samples,
            folds=args.folds,
            seed=args.seed,
            device=args.device,
        )
        result = WalkForwardTransformerTrainer(config).build(bars)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
    except (OSError, ValueError, RuntimeError, pd.errors.ParserError) as exc:
        print(f"Transformer 训练失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
