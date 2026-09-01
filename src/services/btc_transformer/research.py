# -*- coding: utf-8 -*-
"""Research-only multi-seed and one-variable ablation orchestration.

The orchestrator deliberately keeps labels, feature construction and
walk-forward windows fixed across every candidate.  Its payload is suitable
for offline comparison only; it is never a production forecast or promotion
input.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from .features import build_transformer_feature_frame
from .trainer import TransformerTrainingConfig, WalkForwardTransformerTrainer


DEFAULT_RESEARCH_SEEDS = (7, 13, 29, 43, 71)
MIN_RESEARCH_SEEDS = 5
MIN_RESEARCH_EPOCHS = 20
SUPPORTED_ARCHITECTURES = ("patchtst", "itransformer", "fusion")


def _signature(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _label_signature(frame: pd.DataFrame) -> str:
    columns = ["date", *sorted(column for column in frame.columns if column.startswith("target_"))]
    available = [column for column in columns if column in frame.columns]
    payload = frame[available].to_json(orient="split", date_format="iso", double_precision=15)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metric_mean_from_runs(runs: Sequence[Mapping[str, Any]], key: str) -> Optional[float]:
    values = [
        float(summary[key])
        for run in runs
        for summary in (run.get("evaluations", {}) or {}).values()
        if isinstance(summary, Mapping) and summary.get(key) is not None
    ]
    return sum(values) / len(values) if values else None


def _normalise_architectures(architectures: Iterable[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(item).strip().lower() for item in architectures if str(item).strip()))
    if not values:
        values = SUPPORTED_ARCHITECTURES
    unsupported = [item for item in values if item not in SUPPORTED_ARCHITECTURES]
    if unsupported:
        raise ValueError(f"unsupported architectures: {unsupported}")
    return values


def _normalise_seeds(seeds: Sequence[int]) -> tuple[int, ...]:
    values = tuple(dict.fromkeys(int(seed) for seed in seeds))
    if len(values) < MIN_RESEARCH_SEEDS:
        raise ValueError(f"research requires at least {MIN_RESEARCH_SEEDS} distinct seeds")
    return values


def run_research_experiment(
    bars: pd.DataFrame,
    *,
    config: TransformerTrainingConfig,
    architectures: Sequence[str] = SUPPORTED_ARCHITECTURES,
    seeds: Sequence[int] = DEFAULT_RESEARCH_SEEDS,
    ablation_features: Optional[Sequence[str]] = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Run fixed-protocol multi-seed candidates and one-variable ablations.

    Every run receives the same ``TransformerFeatureConfig`` and therefore the
    same target labels, sequence length, purge gap and validation window.  An
    ablation removes exactly one feature from that shared feature set.
    """

    if int(config.epochs) < MIN_RESEARCH_EPOCHS:
        raise ValueError(f"research requires at least {MIN_RESEARCH_EPOCHS} epochs")
    normalized_seeds = _normalise_seeds(seeds)
    normalized_architectures = _normalise_architectures(architectures)

    feature_frame = build_transformer_feature_frame(bars, config=config.feature, as_of=as_of)
    available_features = tuple(column for column in feature_frame.columns if column.startswith("feature_"))
    # Materialize one common complete feature frame.  Reusing it for every
    # candidate prevents an ablated column's NaNs from changing the sample
    # count, labels, or validation-window origins.
    common_feature_frame = feature_frame.dropna(subset=list(available_features)).reset_index(drop=True)
    label_signature = _label_signature(common_feature_frame)
    requested_ablation = tuple(dict.fromkeys(str(item) for item in (ablation_features or ()) if str(item)))
    unknown = [item for item in requested_ablation if item not in available_features]
    if unknown:
        raise ValueError(f"unknown ablation features: {unknown}")

    feature_sets: list[tuple[str, Optional[str], tuple[str, ...]]] = [("full", None, available_features)]
    for feature in requested_ablation:
        feature_sets.append(("ablation", feature, tuple(item for item in available_features if item != feature)))

    runs: list[dict[str, Any]] = []
    validation_window_signature: Optional[str] = None
    for architecture in normalized_architectures:
        for feature_set_name, removed_feature, selected_features in feature_sets:
            for seed in normalized_seeds:
                run_config = TransformerTrainingConfig(
                    feature=config.feature,
                    architecture=architecture,
                    patch_length=config.patch_length,
                    stride=config.stride,
                    d_model=config.d_model,
                    n_heads=config.n_heads,
                    layers=config.layers,
                    dropout=config.dropout,
                    epochs=config.epochs,
                    batch_size=config.batch_size,
                    learning_rate=config.learning_rate,
                    weight_decay=config.weight_decay,
                    folds=config.folds,
                    min_train_samples=config.min_train_samples,
                    validation_samples=config.validation_samples,
                    purge_samples=config.purge_samples,
                    seed=seed,
                    device=config.device,
                    class_weighted_loss=config.class_weighted_loss,
                    class_weight_power=config.class_weight_power,
                    target_clip_sigma=config.target_clip_sigma,
                    return_loss_weight=config.return_loss_weight,
                    volatility_loss_weight=config.volatility_loss_weight,
                    direction_loss_weight=config.direction_loss_weight,
                    regime_loss_weight=config.regime_loss_weight,
                    direction_consistency_weight=config.direction_consistency_weight,
                    trading_cost_bps=config.trading_cost_bps,
                    min_signal_edge_bps=config.min_signal_edge_bps,
                    signal_confidence_threshold=config.signal_confidence_threshold,
                )
                result = WalkForwardTransformerTrainer(run_config).build(
                    bars,
                    as_of=as_of,
                    feature_columns=selected_features,
                    feature_frame=common_feature_frame,
                )
                run_window_signature = _signature((result.get("walk_forward") or {}).get("folds", []))
                if validation_window_signature is None:
                    validation_window_signature = run_window_signature
                elif run_window_signature != validation_window_signature:
                    raise RuntimeError("research validation windows changed between candidates")
                run_oof = result.get("oof_predictions", {})
                oof_prediction_count = sum(
                    len(rows) for rows in run_oof.values() if isinstance(rows, list)
                ) if isinstance(run_oof, Mapping) else 0
                runs.append(
                    {
                        "architecture": architecture,
                        "seed": seed,
                        "feature_set": feature_set_name,
                        "removed_feature": removed_feature,
                        "feature_count": len(selected_features),
                        "feature_columns": list(selected_features),
                        "data_quality": result.get("data_quality"),
                        "evaluations": result.get("evaluations", {}),
                        "oof_predictions": run_oof,
                        "oof_prediction_count": oof_prediction_count,
                        "label_signature": label_signature,
                        "validation_window_signature": run_window_signature,
                    }
                )

    summary: dict[str, Any] = {}
    for architecture in normalized_architectures:
        architecture_runs = [item for item in runs if item["architecture"] == architecture]
        summary[architecture] = {}
        for feature_set_name, removed_feature, _ in feature_sets:
            selected = [
                item for item in architecture_runs
                if item["feature_set"] == feature_set_name and item["removed_feature"] == removed_feature
            ]
            summary[architecture][removed_feature or "full"] = {
                "runs": len(selected),
                "available_runs": sum(item.get("data_quality") == "available" for item in selected),
                "mean_direction_accuracy": _metric_mean_from_runs(selected, "direction_accuracy"),
                "mean_return_mae": _metric_mean_from_runs(selected, "return_mae"),
                "mean_pearson_ic": _metric_mean_from_runs(selected, "pearson_ic"),
                "mean_spearman_ic": _metric_mean_from_runs(selected, "spearman_ic"),
            }

    return {
        "mode": "offline_research_experiment",
        "research_only": True,
        "participates_in_decision": False,
        "eligible_for_promotion": False,
        "promotion_eligible": False,
        "protocol": {
            "same_labels": True,
            "same_validation_window": True,
            "label_signature": label_signature,
            "validation_window_signature": validation_window_signature,
            "ablation": "one_variable_at_a_time",
            "seed_count": len(normalized_seeds),
            "seeds": list(normalized_seeds),
            "epochs": int(config.epochs),
            "validation_samples": int(config.validation_samples),
            "purge_samples": int(config.purge_samples),
            "horizons": dict(config.feature.horizons),
            "neutral_bands": dict(config.feature.neutral_bands),
            "architectures": list(normalized_architectures),
            "feature_count": len(available_features),
            "feature_columns": list(available_features),
            "ablation_features": list(requested_ablation),
        },
        "validity_decision": {
            "status": "pending_review",
            "reason": "Required multi-seed/epoch evidence is collected; structure effectiveness still requires human review of the fixed-window metrics.",
            "minimum_seeds_met": len(normalized_seeds) >= MIN_RESEARCH_SEEDS,
            "minimum_epochs_met": int(config.epochs) >= MIN_RESEARCH_EPOCHS,
            "automatic_promotion": False,
        },
        "summary": summary,
        "runs": runs,
    }


def save_research_artifacts(
    payload: Mapping[str, Any],
    summary_path: Path,
    *,
    oof_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Persist a compact summary and one JSONL row per OOF sample.

    OOF rows are kept outside the task result so polling the Web endpoint does
    not repeatedly transfer the full prediction set.  Both files remain under
    the research-only namespace selected by the caller.
    """

    summary_target = Path(summary_path)
    oof_target = Path(oof_path) if oof_path is not None else summary_target.with_name(f"{summary_target.stem}-oof.jsonl")
    summary_target.parent.mkdir(parents=True, exist_ok=True)
    oof_target.parent.mkdir(parents=True, exist_ok=True)

    compact_runs: list[dict[str, Any]] = []
    total_oof = 0
    with oof_target.open("w", encoding="utf-8", newline="\n") as output:
        for run_index, raw_run in enumerate(payload.get("runs", [])):
            run = dict(raw_run) if isinstance(raw_run, Mapping) else {}
            predictions = run.pop("oof_predictions", {})
            run_count = 0
            if isinstance(predictions, Mapping):
                for horizon, rows in predictions.items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, Mapping):
                            continue
                        output.write(json.dumps({
                            "run_index": run_index,
                            "architecture": run.get("architecture"),
                            "seed": run.get("seed"),
                            "feature_set": run.get("feature_set"),
                            "removed_feature": run.get("removed_feature"),
                            "horizon": horizon,
                            **dict(row),
                        }, ensure_ascii=False, separators=(",", ":")) + "\n")
                        run_count += 1
            run["oof_prediction_count"] = run_count
            compact_runs.append(run)
            total_oof += run_count

    compact_payload = dict(payload)
    compact_payload["runs"] = compact_runs
    compact_payload["mode"] = "offline_research_experiment"
    compact_payload["research_only"] = True
    compact_payload["participates_in_decision"] = False
    compact_payload["eligible_for_promotion"] = False
    compact_payload["promotion_eligible"] = False
    compact_payload["oof_prediction_count"] = total_oof
    compact_payload["oof_artifact_path"] = str(oof_target)
    compact_payload["artifact_path"] = str(summary_target)
    summary_target.write_text(
        json.dumps(compact_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return compact_payload


__all__ = [
    "DEFAULT_RESEARCH_SEEDS",
    "MIN_RESEARCH_EPOCHS",
    "MIN_RESEARCH_SEEDS",
    "SUPPORTED_ARCHITECTURES",
    "run_research_experiment",
    "save_research_artifacts",
]
