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
    DEFAULT_RESEARCH_SEEDS,
    DEFAULT_NEUTRAL_BANDS,
    TransformerFeatureConfig,
    TransformerTrainingConfig,
    WalkForwardTransformerTrainer,
    run_research_experiment,
)


DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")
DEFAULT_HOURLY_HORIZONS = {"1h": 1, "4h": 4, "24h": 24}
DEFAULT_NEUTRAL_BANDS_BPS = {name: value * 10000.0 for name, value in DEFAULT_NEUTRAL_BANDS.items()}


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


def _parse_neutral_bands(value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in str(value or "").split(","):
        name, separator, bps = item.partition(":")
        if not separator or not name.strip():
            raise argparse.ArgumentTypeError("neutral bands 应为 horizon:bps,horizon:bps，例如 1h:20,4h:40,24h:100")
        try:
            result[name.strip()] = max(0.0, float(bps))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效 neutral band bps: {bps}") from exc
    return result


def _parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item.strip()) for item in str(value or "").split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seeds 应为逗号分隔的整数，例如 7,13,29,43,71") from exc
    if len(set(seeds)) < 5:
        raise argparse.ArgumentTypeError("研究模式至少需要 5 个不同 seed")
    return list(dict.fromkeys(seeds))


def _parse_ablation_features(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in str(value or "").split(",") if item.strip()))


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
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--min-train-samples", type=int, default=5000)
    parser.add_argument("--validation-samples", type=int, default=168)
    parser.add_argument("--purge-samples", type=int, default=48)
    parser.add_argument("--folds", type=int, default=12)
    parser.add_argument("--bar-hours", type=float, default=1.0, help="每根 K 线覆盖小时数，5m 数据为 0.0833333。")
    parser.add_argument("--neutral-band-bps", type=float, default=35.0, help="方向标签的中性区间（基点），区间内不产生多空标签。")
    parser.add_argument(
        "--neutral-band-bps-by-horizon",
        type=_parse_neutral_bands,
        default=dict(DEFAULT_NEUTRAL_BANDS_BPS),
        help="按 horizon 覆盖中性区间，例如 1h:35,4h:70,24h:100；未列出的 horizon 使用 --neutral-band-bps。",
    )
    parser.add_argument("--class-weighted-loss", action="store_true", help="显式启用方向/regime 逆频率类别权重；默认关闭，便于先观察未加权基线。")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--device", default="cpu", help="PyTorch 设备，例如 cpu、cuda 或 cuda:0；默认 cpu。")
    parser.add_argument("--target-clip-sigma", type=float, default=5.0, help="回归目标按训练折稳健缩放后的裁剪范围，默认 ±5。")
    parser.add_argument("--trading-cost-bps", type=float, default=10.0, help="交易成本（往返基点），仅用于交易信号过滤和评估。")
    parser.add_argument("--min-signal-edge-bps", type=float, default=5.0, help="除交易成本外要求的最小预期收益缓冲（基点）。")
    parser.add_argument("--signal-confidence-threshold", type=float, default=0.55, help="方向概率进入交易信号所需的最低置信度。")
    parser.add_argument("--direction-consistency-weight", type=float, default=0.0, help="收益回归与方向概率一致性损失的权重；默认关闭，避免长周期噪声压制方向分类。")
    parser.add_argument("--research", action="store_true", help="运行至少 5 个 seed 的固定窗口研究并保存 OOF 预测。")
    parser.add_argument("--seeds", type=_parse_seeds, default=list(DEFAULT_RESEARCH_SEEDS), help="研究模式 seed 列表，至少 5 个不同值。")
    parser.add_argument("--ablation-features", type=_parse_ablation_features, default=[], help="研究模式下按逗号指定单变量消融特征。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        bars = pd.read_csv(args.input)
        feature_config = TransformerFeatureConfig(
            horizons=args.horizons,
            sequence_length=args.sequence_length,
            bar_hours=args.bar_hours,
            neutral_band=max(0.0, args.neutral_band_bps) / 10000.0,
            neutral_bands={name: value / 10000.0 for name, value in args.neutral_band_bps_by_horizon.items()}
            if args.neutral_band_bps_by_horizon
            else {},
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
            class_weighted_loss=args.class_weighted_loss,
            target_clip_sigma=args.target_clip_sigma,
            trading_cost_bps=args.trading_cost_bps,
            min_signal_edge_bps=args.min_signal_edge_bps,
            signal_confidence_threshold=args.signal_confidence_threshold,
            direction_consistency_weight=args.direction_consistency_weight,
        )
        if args.research:
            result = run_research_experiment(
                bars,
                config=config,
                architectures=(args.architecture,),
                seeds=args.seeds,
                ablation_features=args.ablation_features,
            )
        else:
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
