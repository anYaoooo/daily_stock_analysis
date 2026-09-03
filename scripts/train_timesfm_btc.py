#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fine-tune the TimesFM 2.5 PyTorch checkpoint on historical BTC bars.

This is an offline research trainer.  It keeps the pretrained TimesFM
backbone frozen by default and optimizes its forecast output projections with
relative-price Huber loss.  It never writes to the application database or
changes trading decisions.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_INPUT = Path("data/btc_okx_perpetual_1h_training.csv")
DEFAULT_MODEL = "google/timesfm-2.5-200m-pytorch"


def _require_torch() -> Any:
    try:
        import torch
        from torch import nn
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("PyTorch is required; install requirements-ml.txt") from exc
    return torch, nn


def _load_bars(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"date", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"CSV missing required columns: {', '.join(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame = frame[frame["close"] > 0].drop_duplicates("date", keep="last")
    return frame.reset_index(drop=True)


def _sample_indices(indices: Sequence[int], limit: Optional[int]) -> np.ndarray:
    values = np.asarray(list(indices), dtype=np.int64)
    if limit is None or limit <= 0 or len(values) <= limit:
        return values
    selected = np.linspace(0, len(values) - 1, num=int(limit), dtype=np.int64)
    return values[np.unique(selected)]


def _set_trainable(model: Any, scope: str) -> list[Any]:
    for parameter in model.parameters():
        parameter.requires_grad = False
    if scope == "all":
        selected = list(model.parameters())
    else:
        selected = list(model.output_projection_point.parameters())
        if scope == "head":
            selected += list(model.output_projection_quantiles.parameters())
    for parameter in selected:
        parameter.requires_grad = True
    return [parameter for parameter in selected if parameter.requires_grad]


def _differentiable_forecast(model: Any, inputs: Any, horizon: int) -> Any:
    """Run the TimesFM prefill path without the inference-only no_grad wrapper."""
    import timesfm.timesfm_2p5.timesfm_2p5_torch as timesfm_torch

    torch = timesfm_torch.torch
    batch_size, context = inputs.shape
    patch = int(model.p)
    if context % patch:
        raise ValueError(f"context length must be a multiple of TimesFM patch size {patch}")
    patched = inputs.reshape(batch_size, -1, patch)
    masks = torch.zeros_like(patched, dtype=torch.bool)
    n = torch.zeros(batch_size, device=inputs.device)
    mu = torch.zeros(batch_size, device=inputs.device)
    sigma = torch.zeros(batch_size, device=inputs.device)
    patch_mu = []
    patch_sigma = []
    for index in range(patched.shape[1]):
        (n, mu, sigma), _ = timesfm_torch.util.update_running_stats(
            n, mu, sigma, patched[:, index], masks[:, index]
        )
        patch_mu.append(mu)
        patch_sigma.append(sigma)
    context_mu = torch.stack(patch_mu, dim=1)
    context_sigma = torch.stack(patch_sigma, dim=1)
    normed = timesfm_torch.revin(patched, context_mu, context_sigma, reverse=False)
    (_, _, normed_output, _), _ = model(normed, masks)
    output = timesfm_torch.revin(normed_output, context_mu, context_sigma, reverse=True)
    output = output.reshape(batch_size, -1, model.o, model.q)
    return output[:, -1, :horizon, :]


def _loss_and_metrics(prediction: Any, target: Any, base: Any, torch: Any) -> tuple[Any, dict[str, Any]]:
    point = prediction[..., 5]
    pred_return = point / base.unsqueeze(1) - 1.0
    target_return = target / base.unsqueeze(1) - 1.0
    loss = torch.nn.functional.smooth_l1_loss(pred_return, target_return)
    with torch.no_grad():
        actual = target_return.detach().cpu().numpy()
        predicted = pred_return.detach().cpu().numpy()
        metrics = {
            "loss": float(loss.detach().cpu()),
            "direction_accuracy": float(np.mean((predicted >= 0) == (actual >= 0))),
            "mae_pct": float(np.mean(np.abs(predicted - actual)) * 100.0),
        }
    return loss, metrics


def _run_epoch(
    model: Any,
    closes: np.ndarray,
    indices: np.ndarray,
    *,
    context: int,
    horizon: int,
    batch_size: int,
    torch: Any,
    optimizer: Optional[Any] = None,
) -> dict[str, float]:
    training = optimizer is not None
    model.train(training)
    totals = {"loss": 0.0, "direction_accuracy": 0.0, "mae_pct": 0.0, "samples": 0}
    for start in range(0, len(indices), batch_size):
        batch_indices = indices[start : start + batch_size]
        context_values = np.stack([closes[i - context + 1 : i + 1] for i in batch_indices]).astype(np.float32)
        target_values = np.stack([closes[i + 1 : i + horizon + 1] for i in batch_indices]).astype(np.float32)
        parameter_device = next(model.parameters()).device
        inputs = torch.from_numpy(context_values).to(parameter_device)
        target = torch.from_numpy(target_values).to(inputs.device)
        base = inputs[:, -1]
        if training:
            optimizer.zero_grad(set_to_none=True)
        prediction = _differentiable_forecast(model, inputs, horizon)
        loss, metrics = _loss_and_metrics(prediction, target, base, torch)
        if training:
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            optimizer.step()
        count = len(batch_indices)
        totals["loss"] += metrics["loss"] * count
        totals["direction_accuracy"] += metrics["direction_accuracy"] * count
        totals["mae_pct"] += metrics["mae_pct"] * count
        totals["samples"] += count
    sample_count = max(1, int(totals.pop("samples")))
    return {name: value / sample_count for name, value in totals.items()}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微调 TimesFM 2.5 的 BTC 历史预测输出头。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--context-length", type=int, default=512)
    parser.add_argument("--horizon-bars", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--purge-bars", type=int, default=24)
    parser.add_argument("--max-train-samples", type=int, default=0)
    parser.add_argument("--max-validation-samples", type=int, default=0)
    parser.add_argument("--trainable-scope", choices=("projection", "head", "all"), default="projection")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path, default=Path("artifacts/timesfm-btc-finetune.json"))
    parser.add_argument("--checkpoint", type=Path, default=Path("artifacts/timesfm-btc-finetune.pt"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    torch, _ = _require_torch()
    if args.context_length < 32 or args.context_length % 32:
        raise SystemExit("--context-length must be a positive multiple of 32")
    if args.horizon_bars < 1 or args.horizon_bars > 128:
        raise SystemExit("--horizon-bars must be between 1 and 128")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    bars = _load_bars(args.input)
    closes = bars["close"].to_numpy(dtype=np.float32)
    split = int(len(closes) * min(max(float(args.validation_ratio), 0.05), 0.5))
    split = len(closes) - split
    first = args.context_length - 1
    train_last = split - max(args.horizon_bars, args.purge_bars) - 1
    train_indices = _sample_indices(range(first, max(first, train_last + 1)), args.max_train_samples or None)
    validation_indices = _sample_indices(range(split, len(closes) - args.horizon_bars), args.max_validation_samples or None)
    if len(train_indices) == 0 or len(validation_indices) == 0:
        raise SystemExit("not enough BTC bars for the requested context/horizon split")

    import timesfm

    kwargs = {"torch_compile": False, "local_files_only": True}
    if args.cache_dir:
        kwargs["cache_dir"] = args.cache_dir
    wrapper = timesfm.TimesFM_2p5_200M_torch.from_pretrained(args.model_id, **kwargs)
    device = torch.device(args.device)
    wrapper.model.to(device)
    trainable = _set_trainable(wrapper.model, args.trainable_scope)
    optimizer = torch.optim.AdamW(trainable, lr=max(float(args.learning_rate), 1e-8), weight_decay=max(float(args.weight_decay), 0.0))

    history = []
    for epoch in range(1, max(1, int(args.epochs)) + 1):
        train_metrics = _run_epoch(wrapper.model, closes, train_indices, context=args.context_length, horizon=args.horizon_bars, batch_size=max(1, args.batch_size), torch=torch, optimizer=optimizer)
        with torch.no_grad():
            validation_metrics = _run_epoch(wrapper.model, closes, validation_indices, context=args.context_length, horizon=args.horizon_bars, batch_size=max(1, args.batch_size), torch=torch)
        row = {"epoch": epoch, "train": train_metrics, "validation": validation_metrics}
        history.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_id": args.model_id,
            "model_version": "timesfm-2.5-200m-pytorch-btc-finetuned",
            "context_length": args.context_length,
            "horizon_bars": args.horizon_bars,
            "trainable_scope": args.trainable_scope,
            "state_dict": wrapper.model.state_dict(),
            "history": history,
        },
        args.checkpoint,
    )
    result = {
        "data_quality": "available",
        "model_id": args.model_id,
        "trainable_scope": args.trainable_scope,
        "device": str(device),
        "source_bars": int(len(closes)),
        "date_start": bars["date"].iloc[0].isoformat(),
        "date_end": bars["date"].iloc[-1].isoformat(),
        "context_length": args.context_length,
        "horizon_bars": args.horizon_bars,
        "train_samples": int(len(train_indices)),
        "validation_samples": int(len(validation_indices)),
        "split_index": int(split),
        "purge_bars": int(max(args.horizon_bars, args.purge_bars)),
        "history": history,
        "checkpoint": str(args.checkpoint),
        "participates_in_decision": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "checkpoint": str(args.checkpoint), "final_validation": history[-1]["validation"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
