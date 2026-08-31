# -*- coding: utf-8 -*-
"""Offline BTC Transformer research endpoints.

These routes are intentionally isolated from analysis, trading and promotion
paths. They submit a background experiment and expose its compact summary plus
a separately downloadable per-sample OOF artifact.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.v1.schemas.btc_training import (
    BtcTrainingConfigResponse,
    BtcTrainingRunRequest,
    BtcTrainingTaskAccepted,
    BtcTrainingTaskStatus,
)
from src.services.btc_transformer import (
    DEFAULT_RESEARCH_SEEDS,
    MIN_RESEARCH_EPOCHS,
    MIN_RESEARCH_SEEDS,
    SUPPORTED_ARCHITECTURES,
    TransformerFeatureConfig,
    TransformerTrainingConfig,
    build_transformer_feature_frame,
    run_research_experiment,
    save_research_artifacts,
)
from src.services.task_queue import TaskStatus, get_task_queue

logger = logging.getLogger(__name__)
router = APIRouter()

_DEFAULT_INPUT = Path(__file__).resolve().parents[3] / "data" / "btc_okx_perpetual_1h_training.csv"
# Keep research payloads in a separate namespace; production code must never
# discover them through the existing model artifact names.
_ARTIFACT_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "research"


def _architectures(value: str) -> tuple[str, ...]:
    return SUPPORTED_ARCHITECTURES if value == "all" else (value,)


def _validate_request(request: BtcTrainingRunRequest) -> None:
    seeds = tuple(dict.fromkeys(int(seed) for seed in request.seeds))
    if len(seeds) < MIN_RESEARCH_SEEDS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": f"至少需要 {MIN_RESEARCH_SEEDS} 个不同 seed"},
        )
    if request.epochs < MIN_RESEARCH_EPOCHS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": f"至少需要 {MIN_RESEARCH_EPOCHS} epoch"},
        )


def _feature_catalog() -> list[str]:
    if not _DEFAULT_INPUT.is_file():
        return []
    try:
        frame = pd.read_csv(_DEFAULT_INPUT)
        feature_frame = build_transformer_feature_frame(
            frame,
            config=TransformerFeatureConfig(horizons={"1h": 1}, sequence_length=32, bar_hours=1.0),
        )
        return [column for column in feature_frame.columns if column.startswith("feature_")]
    except Exception as exc:  # pragma: no cover - diagnostics endpoint is fail-soft
        logger.warning("读取 BTC 训练特征目录失败: %s", exc)
        return []


@router.get("/config", response_model=BtcTrainingConfigResponse, summary="获取 BTC 训练研究配置")
def get_btc_training_config() -> BtcTrainingConfigResponse:
    features = _feature_catalog()
    return BtcTrainingConfigResponse(
        architectures=list(SUPPORTED_ARCHITECTURES),
        default_seeds=list(DEFAULT_RESEARCH_SEEDS),
        min_seeds=MIN_RESEARCH_SEEDS,
        min_epochs=MIN_RESEARCH_EPOCHS,
        feature_count=len(features),
        feature_columns=features,
    )


@router.post(
    "/run",
    status_code=202,
    response_model=BtcTrainingTaskAccepted,
    summary="提交 BTC 多 seed 训练与消融研究",
    description="固定标签和验证窗口，至少五个 seed、二十个 epoch；结果仅用于离线研究。",
)
def start_btc_training(request: BtcTrainingRunRequest) -> BtcTrainingTaskAccepted:
    _validate_request(request)
    if not _DEFAULT_INPUT.is_file():
        raise HTTPException(status_code=503, detail={"error": "data_unavailable", "message": "BTC 训练数据文件不存在"})
    available_features = set(_feature_catalog())
    unknown_features = sorted(set(request.ablation_features) - available_features)
    if unknown_features:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": f"未知消融特征: {', '.join(unknown_features)}"},
        )

    seeds = tuple(dict.fromkeys(int(seed) for seed in request.seeds))
    architectures = _architectures(request.architecture)
    protocol = {
        "same_labels": True,
        "same_validation_window": True,
        "seed_count": len(seeds),
        "epochs": request.epochs,
        "validation_samples": request.validation_samples,
        "purge_samples": request.purge_samples,
        "horizons": {"1h": 1, "4h": 4, "24h": 24},
        "architectures": list(architectures),
        "feature_count": len(available_features),
        "research_only": True,
        "promotion_eligible": False,
    }

    task_id = uuid.uuid4().hex

    def run_task() -> dict[str, Any]:
        bars = pd.read_csv(_DEFAULT_INPUT)
        feature_config = TransformerFeatureConfig(
            horizons={"1h": 1, "4h": 4, "24h": 24},
            sequence_length=request.sequence_length,
            bar_hours=1.0,
        )
        config = TransformerTrainingConfig(
            feature=feature_config,
            architecture=architectures[0],
            epochs=request.epochs,
            folds=request.folds,
            min_train_samples=request.min_train_samples,
            validation_samples=request.validation_samples,
            purge_samples=request.purge_samples,
            seed=seeds[0],
            device="cpu",
        )
        result = run_research_experiment(
            bars,
            config=config,
            architectures=architectures,
            seeds=seeds,
            ablation_features=request.ablation_features,
        )
        artifact_path = _ARTIFACT_DIR / f"btc-training-research-{task_id}.json"
        oof_artifact_path = _ARTIFACT_DIR / f"btc-training-research-{task_id}-oof.jsonl"
        result["artifact_path"] = str(artifact_path)
        result["oof_artifact_path"] = str(oof_artifact_path)
        result["artifact_role"] = "research_only"
        result["promotion_eligible"] = False
        result["eligible_for_promotion"] = False
        return save_research_artifacts(result, artifact_path, oof_path=oof_artifact_path)

    task = get_task_queue().submit_background_task(
        run_task,
        stock_code="btc_training",
        stock_name="BTC 训练研究",
        report_type="btc_training",
        message="训练研究已加入队列（仅离线研究）",
        task_id=task_id,
    )
    return BtcTrainingTaskAccepted(
        task_id=task.task_id,
        status=task.status.value,
        message=task.message,
        protocol=protocol,
    )


@router.get("/tasks/{task_id}", response_model=BtcTrainingTaskStatus, summary="查询 BTC 训练研究任务")
def get_btc_training_task(task_id: str) -> BtcTrainingTaskStatus:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "btc_training":
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "BTC 训练研究任务不存在或已过期"})
    result = task.result if task.status == TaskStatus.COMPLETED and isinstance(task.result, dict) else None
    return BtcTrainingTaskStatus(
        task_id=task.task_id,
        status=task.status.value,
        progress=task.progress,
        message=task.message,
        result=result,
        error=task.error,
    )


@router.get(
    "/tasks/{task_id}/artifacts/{artifact_kind}",
    response_class=FileResponse,
    summary="下载 BTC 训练研究产物",
)
def download_btc_training_artifact(
    task_id: str,
    artifact_kind: Literal["summary", "oof"],
) -> FileResponse:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "btc_training":
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "BTC 训练研究任务不存在或已过期"})
    if task.status != TaskStatus.COMPLETED or not isinstance(task.result, dict):
        raise HTTPException(status_code=409, detail={"error": "not_ready", "message": "BTC 训练研究产物尚未生成"})

    result_key = "artifact_path" if artifact_kind == "summary" else "oof_artifact_path"
    raw_path = task.result.get(result_key)
    if not isinstance(raw_path, str) or not raw_path:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "训练研究产物不存在"})
    artifact_path = Path(raw_path).resolve()
    research_root = _ARTIFACT_DIR.resolve()
    try:
        artifact_path.relative_to(research_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "训练研究产物路径无效"}) from exc
    if not artifact_path.is_file():
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "训练研究产物文件不存在"})
    media_type = "application/x-ndjson" if artifact_kind == "oof" else "application/json"
    return FileResponse(path=artifact_path, filename=artifact_path.name, media_type=media_type)


__all__ = ["router"]
