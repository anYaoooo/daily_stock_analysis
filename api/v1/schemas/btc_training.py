# -*- coding: utf-8 -*-
"""Schemas for the offline BTC model research workspace."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class BtcTrainingRunRequest(BaseModel):
    architecture: Literal["patchtst", "itransformer", "fusion", "all"] = "patchtst"
    epochs: int = Field(30, ge=20, le=200, description="Research runs default to 30 epochs; at least 20 are required")
    seeds: List[int] = Field(
        default_factory=lambda: [7, 13, 29, 43, 71],
        min_length=5,
        max_length=20,
        description="At least five distinct seeds are required before judging a structure",
    )
    sequence_length: int = Field(256, ge=8, le=2048)
    folds: int = Field(12, ge=1, le=50)
    min_train_samples: int = Field(5000, ge=24, le=200000)
    validation_samples: int = Field(168, ge=1, le=20000)
    purge_samples: int = Field(48, ge=1, le=20000)
    ablation_features: List[str] = Field(default_factory=list, max_length=128)


class BtcTrainingTaskAccepted(BaseModel):
    task_id: str
    status: str
    message: Optional[str] = None
    protocol: Dict[str, Any] = Field(default_factory=dict)


class BtcTrainingTaskStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class BtcTrainingConfigResponse(BaseModel):
    architectures: List[str]
    default_seeds: List[int]
    min_seeds: int
    min_epochs: int
    feature_count: int
    feature_columns: List[str]
    research_only: bool = True
    promotion_eligible: bool = False
