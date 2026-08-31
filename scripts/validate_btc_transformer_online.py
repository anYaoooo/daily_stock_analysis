#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train BTC Transformer candidates before a cutoff and score the next bars.

This is an online-style holdout: the model only sees bars closed at the
cutoff, then its forecast is compared with the already-realized bars after the
cutoff. It is intentionally separate from the production decision path.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.services.btc_transformer import (  # noqa: E402
    DEFAULT_NEUTRAL_BANDS,
    TransformerFeatureConfig,
    TransformerTrainingConfig,
    WalkForwardTransformerTrainer,
)


DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")
DEFAULT_OUTPUT = Path("artifacts/btc-transformer-online-validation.json")
DEFAULT_HORIZONS = {"1h": 1, "4h": 4, "24h": 24}
DEFAULT_NEUTRAL_BANDS_BPS = {name: value * 10000.0 for name, value in DEFAULT_NEUTRAL_BANDS.items()}


def _parse_horizons(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in str(value or "").split(","):
        name, separator, bars = item.partition(":")
        if not separator or not name.strip():
            raise argparse.ArgumentTypeError("horizons 应为 name:bars,name:bars")
        try:
            parsed = int(bars)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效 horizon bars: {bars}") from exc
        if parsed < 1:
            raise argparse.ArgumentTypeError("horizon bars 必须为正整数")
        result[name.strip()] = parsed
    if not result:
        raise argparse.ArgumentTypeError("至少需要一个 horizon")
    return result


def _parse_neutral_bands(value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in str(value or "").split(","):
        name, separator, bps = item.partition(":")
        if not separator or not name.strip():
            raise argparse.ArgumentTypeError("neutral bands 应为 horizon:bps,horizon:bps")
        try:
            result[name.strip()] = max(0.0, float(bps))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效 neutral band bps: {bps}") from exc
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC Transformer 长周期训练与在线样本外验证。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="包含最新闭合 K 线的 CSV 路径。")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="验证结果 JSON 路径。")
    parser.add_argument("--architecture", choices=("patchtst", "itransformer", "fusion", "all"), default="all")
    parser.add_argument("--horizons", type=_parse_horizons, default=dict(DEFAULT_HORIZONS))
    parser.add_argument("--holdout-hours", type=int, default=24, help="在线验证 cutoff 距最新闭合小时的距离。")
    parser.add_argument("--sequence-length", type=int, default=256)
    parser.add_argument("--patch-length", type=int, default=16)
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--d-model", type=int, default=128)
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--layers", type=int, default=3)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--min-train-samples", type=int, default=5000)
    parser.add_argument("--validation-samples", type=int, default=168)
    parser.add_argument("--purge-samples", type=int, default=48)
    parser.add_argument("--neutral-band-bps", type=float, default=35.0)
    parser.add_argument(
        "--neutral-band-bps-by-horizon",
        type=_parse_neutral_bands,
        default=dict(DEFAULT_NEUTRAL_BANDS_BPS),
        help="按 horizon 覆盖中性区间，例如 1h:35,4h:70,24h:100。",
    )
    parser.add_argument("--target-clip-sigma", type=float, default=5.0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--class-weighted-loss", action="store_true", help="显式启用方向/regime 逆频率类别权重；默认关闭。")
    return parser


def _direction(value: float, neutral_band: float) -> str:
    if value > neutral_band:
        return "up"
    if value < -neutral_band:
        return "down"
    return "neutral"


def _realized_targets(
    bars: pd.DataFrame,
    *,
    cutoff: pd.Timestamp,
    horizons: dict[str, int],
    bar_hours: float,
    neutral_band: float,
    neutral_bands: Optional[Mapping[str, float]] = None,
) -> dict[str, Optional[dict[str, Any]]]:
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last").set_index("date").sort_index()
    targets: dict[str, Optional[dict[str, Any]]] = {}
    for name, bars_ahead in horizons.items():
        future_at = cutoff + pd.Timedelta(hours=float(bar_hours) * int(bars_ahead))
        if cutoff not in frame.index or future_at not in frame.index:
            targets[name] = None
            continue
        start_close = float(frame.at[cutoff, "close"])
        future_close = float(frame.at[future_at, "close"])
        if start_close <= 0.0 or future_close <= 0.0:
            targets[name] = None
            continue
        realized_return = float(math.log(future_close / start_close))
        band = float((neutral_bands or {}).get(name, neutral_band))
        targets[name] = {
            "as_of": str(future_at),
            "return": realized_return,
            "direction": _direction(realized_return, band),
            "start_close": start_close,
            "future_close": future_close,
        }
    return targets


def _score_forecast(forecast: dict[str, Any], realized: Optional[dict[str, Any]], trading_cost_bps: float) -> dict[str, Any]:
    if realized is None:
        return {"available": False, "reason": "future_bar_missing"}
    predicted_direction = str(forecast.get("direction") or "")
    signal = forecast.get("trade_signal") if isinstance(forecast.get("trade_signal"), dict) else {}
    action = str(signal.get("action") or "hold")
    position = 1.0 if action == "long" else -1.0 if action == "short" else 0.0
    net_return = position * float(realized["return"]) - (float(trading_cost_bps) / 10000.0 if position else 0.0)
    return {
        "available": True,
        "predicted_direction": predicted_direction,
        "actual_direction": realized["direction"],
        "direction_correct": predicted_direction == realized["direction"],
        "predicted_return": float(forecast.get("return", 0.0)),
        "actual_return": float(realized["return"]),
        "return_abs_error": abs(float(forecast.get("return", 0.0)) - float(realized["return"])),
        "trade_action": action,
        "net_return": net_return,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.holdout_hours < max(args.horizons.values()):
        print("在线 holdout 小时数必须覆盖最长 horizon", file=sys.stderr)
        return 2
    try:
        bars = pd.read_csv(args.input)
    except (OSError, pd.errors.ParserError) as exc:
        print(f"读取 BTC CSV 失败: {exc}", file=sys.stderr)
        return 1
    if bars.empty or "date" not in bars.columns or "close" not in bars.columns:
        print("BTC CSV 缺少 date/close 或为空", file=sys.stderr)
        return 1
    bars["date"] = pd.to_datetime(bars["date"], utc=True, errors="coerce")
    bars = bars.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    latest_open = pd.Timestamp(bars["date"].iloc[-1])
    cutoff = latest_open - pd.Timedelta(hours=int(args.holdout_hours))
    if cutoff not in set(bars["date"]):
        print(f"cutoff 不在连续小时线上: {cutoff}", file=sys.stderr)
        return 1

    architectures = ("patchtst", "itransformer", "fusion") if args.architecture == "all" else (args.architecture,)
    neutral_band = max(0.0, float(args.neutral_band_bps)) / 10000.0
    neutral_bands = {
        name: max(0.0, float(value)) / 10000.0
        for name, value in (args.neutral_band_bps_by_horizon or {}).items()
    }
    realized = _realized_targets(
        bars,
        cutoff=cutoff,
        horizons=args.horizons,
        bar_hours=1.0,
        neutral_band=neutral_band,
        neutral_bands=neutral_bands,
    )
    results: dict[str, Any] = {}
    for architecture in architectures:
        feature_config = TransformerFeatureConfig(
            horizons=args.horizons,
            sequence_length=args.sequence_length,
            bar_hours=1.0,
            neutral_band=neutral_band,
            neutral_bands=neutral_bands,
        )
        config = TransformerTrainingConfig(
            feature=feature_config,
            architecture=architecture,
            patch_length=args.patch_length,
            stride=args.stride,
            d_model=args.d_model,
            n_heads=args.heads,
            layers=args.layers,
            epochs=args.epochs,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            folds=args.folds,
            min_train_samples=args.min_train_samples,
            validation_samples=args.validation_samples,
            purge_samples=args.purge_samples,
            target_clip_sigma=args.target_clip_sigma,
            device=args.device,
            seed=args.seed,
            class_weighted_loss=args.class_weighted_loss,
            direction_consistency_weight=0.0,
        )
        print(f"[{architecture}] train through {cutoff.isoformat()} ...", flush=True)
        try:
            payload = WalkForwardTransformerTrainer(config).build(bars, as_of=cutoff + pd.Timedelta(hours=1))
        except (RuntimeError, ValueError) as exc:
            results[architecture] = {"status": "failed", "error": str(exc)}
            print(f"[{architecture}] failed: {exc}", file=sys.stderr, flush=True)
            continue
        scored = {
            horizon: _score_forecast(payload.get("forecasts", {}).get(horizon, {}), realized[horizon], config.trading_cost_bps)
            for horizon in args.horizons
        }
        results[architecture] = {
            "status": "available" if payload.get("data_quality") == "available" else payload.get("data_quality"),
            "training": payload.get("training_config", {}),
            "source_sample_count": payload.get("source_sample_count"),
            "forecasts_at_cutoff": payload.get("forecasts", {}),
            "realized_targets": realized,
            "scores": scored,
            "walk_forward": payload.get("walk_forward", {}),
        }
    output = {
        "mode": "offline_trained_online_holdout",
        "research_only": True,
        "participates_in_decision": False,
        "eligible_for_promotion": False,
        "promotion_eligible": False,
        "source": str(args.input),
        "source_latest_closed": str(latest_open),
        "cutoff": str(cutoff),
        "holdout_hours": args.holdout_hours,
        "architectures": list(architectures),
        "results": results,
    }
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"写入验证结果失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "cutoff": str(cutoff), "source_latest_closed": str(latest_open)}, ensure_ascii=False))
    return 0 if any(item.get("status") == "available" for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
