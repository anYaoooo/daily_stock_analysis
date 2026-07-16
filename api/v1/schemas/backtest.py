# -*- coding: utf-8 -*-
"""Backtest API schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from api.v1.schemas.market_phase import MarketPhaseSummary
from src.schemas.decision_action import DecisionAction


class BacktestRunRequest(BaseModel):
    code: Optional[str] = Field(None, description="仅回测指定股票")
    force: bool = Field(False, description="强制重新计算")
    analysis_mode: Optional[str] = Field(None, description="分析周期过滤：daily/hourly")
    eval_window_days: Optional[int] = Field(None, ge=1, le=120, description="评估窗口（交易日数）")
    min_age_days: Optional[int] = Field(None, ge=0, le=365, description="分析记录最小天龄（0=不限）")
    limit: int = Field(200, ge=1, le=2000, description="最多处理的分析记录数")


class CryptoBacktestSelectedRunRequest(BaseModel):
    analysis_history_ids: List[int] = Field(
        default_factory=list,
        description="要回测的分析历史记录主键 ID 列表",
    )
    plan_types: Optional[List[str]] = Field(
        None,
        description="可选计划类型过滤：daily_long/daily_short/intraday",
    )
    force: bool = Field(False, description="强制重新计算已存在的计划回测")


class BacktestRunResponse(BaseModel):
    processed: int = Field(..., description="候选记录数")
    saved: int = Field(..., description="写入回测结果数")
    completed: int = Field(..., description="完成回测数")
    insufficient: int = Field(..., description="数据不足数")
    errors: int = Field(..., description="错误数")


class CryptoBacktestRunResponse(BacktestRunResponse):
    skipped: int = Field(0, description="跳过数")


class BacktestDeleteResponse(BaseModel):
    deleted: int = Field(..., description="删除的回测结果数量")


class BacktestResultItem(BaseModel):
    analysis_history_id: int
    code: str
    stock_name: Optional[str] = None
    analysis_date: Optional[str] = None
    analysis_mode: Optional[str] = None
    analysis_timeframe: Optional[str] = None
    eval_window_days: int
    engine_version: str
    eval_status: str
    evaluated_at: Optional[str] = None
    operation_advice: Optional[str] = None
    action: Optional[DecisionAction] = None
    action_label: Optional[str] = None
    trend_prediction: Optional[str] = None
    market_phase: Optional[str] = None
    market_phase_summary: Optional[MarketPhaseSummary] = None
    position_recommendation: Optional[str] = None
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    max_high: Optional[float] = None
    min_low: Optional[float] = None
    stock_return_pct: Optional[float] = None
    actual_return_pct: Optional[float] = None
    actual_movement: Optional[str] = None
    direction_expected: Optional[str] = None
    direction_correct: Optional[bool] = None
    outcome: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    hit_stop_loss: Optional[bool] = None
    hit_take_profit: Optional[bool] = None
    first_hit: Optional[str] = None
    first_hit_date: Optional[str] = None
    first_hit_trading_days: Optional[int] = None
    simulated_entry_price: Optional[float] = None
    simulated_exit_price: Optional[float] = None
    simulated_exit_reason: Optional[str] = None
    simulated_return_pct: Optional[float] = None


class CryptoBacktestResultItem(BaseModel):
    analysis_history_id: int
    code: str
    analysis_created_at: Optional[str] = None
    evaluated_at: Optional[str] = None
    plan_type: str
    horizon: str
    analysis_mode: Optional[str] = None
    analysis_timeframe: Optional[str] = None
    direction: str
    engine_version: str
    eval_status: str
    evaluation_start: Optional[str] = None
    evaluation_end: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    entry_triggered: Optional[bool] = None
    entry_triggered_at: Optional[str] = None
    direction_correct: Optional[bool] = None
    outcome: Optional[str] = None
    hit_stop_loss: Optional[bool] = None
    hit_take_profit: Optional[bool] = None
    first_hit: Optional[str] = None
    first_hit_at: Optional[str] = None
    simulated_return_pct: Optional[float] = None
    trade: Dict[str, Any] = Field(default_factory=dict)
    execution: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class CryptoBacktestHistoryPlan(BaseModel):
    plan_type: str
    horizon: str
    analysis_mode: Optional[str] = None
    analysis_timeframe: Optional[str] = None
    direction: str
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    execution_contract: Optional[Dict[str, Any]] = None
    invalid_condition: Optional[str] = None
    risk_reward: Optional[str] = None
    position_hint: Optional[str] = None
    confidence: Optional[str] = None
    backtestable: bool
    quality_status: str
    missing_fields: List[str] = Field(default_factory=list)
    no_trade_reason: Optional[str] = None
    backtest_status: str
    latest_result: Optional[CryptoBacktestResultItem] = None
    indicator_tags: Optional[Dict[str, Any]] = None


class CryptoBacktestHistoryItem(BaseModel):
    analysis_history_id: int
    query_id: Optional[str] = None
    code: str
    stock_name: Optional[str] = None
    report_type: Optional[str] = None
    analysis_created_at: Optional[str] = None
    analysis_mode: Optional[str] = None
    analysis_timeframe: Optional[str] = None
    analysis_summary: Optional[str] = None
    operation_advice: Optional[str] = None
    trend_prediction: Optional[str] = None
    backtest_status: str
    plans: List[CryptoBacktestHistoryPlan] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class CryptoBacktestHistoryResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[CryptoBacktestHistoryItem] = Field(default_factory=list)


class BacktestResultsResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[BacktestResultItem] = Field(default_factory=list)


class CryptoBacktestResultsResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[CryptoBacktestResultItem] = Field(default_factory=list)


class PerformanceMetrics(BaseModel):
    scope: str
    code: Optional[str] = None
    eval_window_days: int
    engine_version: str
    computed_at: Optional[str] = None

    total_evaluations: int
    completed_count: int
    insufficient_count: int
    long_count: int
    cash_count: int
    win_count: int
    loss_count: int
    neutral_count: int

    direction_accuracy_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    neutral_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    avg_simulated_return_pct: Optional[float] = None

    stop_loss_trigger_rate: Optional[float] = None
    take_profit_trigger_rate: Optional[float] = None
    ambiguous_rate: Optional[float] = None
    avg_days_to_first_hit: Optional[float] = None

    advice_breakdown: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class CryptoBacktestMetrics(BaseModel):
    scope: str
    code: Optional[str] = None
    horizon: Optional[str] = None
    analysis_mode: Optional[str] = None
    analysis_timeframe: Optional[str] = None
    plan_type: Optional[str] = None
    engine_version: str
    computed_at: Optional[str] = None
    total_evaluations: int
    completed_count: int
    triggered_count: int
    no_entry_count: int
    skipped_count: int
    insufficient_count: int
    win_count: int
    loss_count: int
    neutral_count: int
    direction_accuracy_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    avg_simulated_return_pct: Optional[float] = None
    plan_type_breakdown: Dict[str, Any] = Field(default_factory=dict)
    risk_metrics: Dict[str, Any] = Field(default_factory=dict)
    equity_curve: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
