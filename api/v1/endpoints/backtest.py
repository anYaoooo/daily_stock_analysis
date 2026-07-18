# -*- coding: utf-8 -*-
"""Backtest endpoints."""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.backtest import (
    BacktestDeleteResponse,
    BacktestRunRequest,
    BacktestRunResponse,
    CryptoBacktestMetrics,
    CryptoBacktestHistoryItem,
    CryptoBacktestHistoryResponse,
    CryptoBacktestLossReviewResponse,
    CryptoBacktestResultsResponse,
    CryptoBacktestResultItem,
    CryptoBacktestRunResponse,
    CryptoBacktestSelectedRunRequest,
    BacktestResultItem,
    BacktestResultsResponse,
    PerformanceMetrics,
)
from api.v1.schemas.common import ErrorResponse
from src.services.backtest_service import BacktestService
from src.services.crypto_backtest_service import CryptoBacktestService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()

BacktestAnalysisPhaseQuery = Literal["premarket", "intraday", "postmarket", "unknown"]
BacktestAnalysisModeQuery = Literal["daily", "hourly"]
CryptoBacktestDirectionQuery = Literal["long", "short", "wait"]
CryptoBacktestPlanTypeQuery = Literal["daily_long", "daily_short", "intraday"]
CryptoBacktestResultStatusQuery = Literal[
    "pending",
    "win",
    "loss",
    "neutral",
    "no_entry",
    "skipped",
    "insufficient_data",
    "invalid_plan",
]


def _validate_analysis_date_range(
    analysis_date_from: Optional[date],
    analysis_date_to: Optional[date],
) -> None:
    if analysis_date_from and analysis_date_to and analysis_date_from > analysis_date_to:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_params",
                "message": "analysis_date_from cannot be after analysis_date_to",
            },
        )


@router.post(
    "/run",
    response_model=BacktestRunResponse,
    responses={
        200: {"description": "回测执行完成"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="触发回测",
    description="对历史分析记录进行回测评估，并写入 backtest_results/backtest_summaries",
)
def run_backtest(
    request: BacktestRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestRunResponse:
    try:
        service = BacktestService(db_manager)
        stats = service.run_backtest(
            code=request.code,
            force=request.force,
            analysis_mode=request.analysis_mode,
            eval_window_days=request.eval_window_days,
            min_age_days=request.min_age_days,
            limit=request.limit,
        )
        return BacktestRunResponse(**stats)
    except Exception as exc:
        logger.error(f"回测执行失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"回测执行失败: {str(exc)}"},
        )


@router.post(
    "/crypto/run",
    response_model=CryptoBacktestRunResponse,
    responses={
        200: {"description": "BTC 计划回测执行完成"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="触发 BTC 计划回测",
    description="从 analysis_history 中提取 BTC 日线多/空计划与小时线日内计划，写入 crypto_backtest_results",
)
def run_crypto_backtest(
    request: BacktestRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestRunResponse:
    try:
        service = CryptoBacktestService(db_manager)
        min_age_hours = None
        if request.min_age_days is not None:
            min_age_hours = int(request.min_age_days) * 24
        stats = service.run_backtest(
            code=request.code,
            force=request.force,
            min_age_hours=min_age_hours,
            limit=request.limit,
        )
        return CryptoBacktestRunResponse(**stats)
    except Exception as exc:
        logger.error(f"BTC 回测执行失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"BTC 回测执行失败: {str(exc)}"},
        )


@router.post(
    "/crypto/run-selected",
    response_model=CryptoBacktestRunResponse,
    responses={
        200: {"description": "指定 BTC 历史记录回测执行完成"},
        400: {"description": "请求参数错误", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="按历史记录触发 BTC 计划回测",
    description="根据 analysis_history_id 精确回测已保存报告中的计划，避免用户手动理解底层候选参数。",
)
def run_selected_crypto_backtests(
    request: CryptoBacktestSelectedRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestRunResponse:
    try:
        allowed_plan_types = {"daily_long", "daily_short", "intraday"}
        plan_types = request.plan_types or None
        if plan_types:
            invalid = sorted({item for item in plan_types if item not in allowed_plan_types})
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "invalid_params",
                        "message": f"Unsupported plan_types: {', '.join(invalid)}",
                    },
                )
        service = CryptoBacktestService(db_manager)
        stats = service.run_selected_backtests(
            analysis_history_ids=request.analysis_history_ids,
            plan_types=plan_types,
            force=request.force,
        )
        return CryptoBacktestRunResponse(**stats)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"指定 BTC 回测执行失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"指定 BTC 回测执行失败: {str(exc)}"},
        )


@router.get(
    "/crypto/history",
    response_model=CryptoBacktestHistoryResponse,
    responses={
        200: {"description": "BTC 历史分析记录与回测状态列表"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取 BTC 历史分析记录回测入口",
    description="分页返回 BTC 分析历史、结构化计划可回测状态和最新计划回测摘要。",
)
def get_crypto_backtest_history(
    code: Optional[str] = Query(None, description="BTC 代码筛选"),
    analysis_mode: Optional[BacktestAnalysisModeQuery] = Query(None, description="分析周期过滤：daily/hourly"),
    direction: Optional[CryptoBacktestDirectionQuery] = Query(None, description="计划方向过滤：long/short/wait"),
    plan_type: Optional[CryptoBacktestPlanTypeQuery] = Query(None, description="计划类型过滤"),
    result_status: Optional[CryptoBacktestResultStatusQuery] = Query(None, description="回测状态过滤"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestHistoryResponse:
    try:
        service = CryptoBacktestService(db_manager)
        data = service.get_history_records(
            code=code,
            analysis_mode=analysis_mode,
            direction=direction,
            plan_type=plan_type,
            result_status=result_status,
            page=page,
            limit=limit,
        )
        return CryptoBacktestHistoryResponse(
            total=int(data.get("total", 0)),
            page=page,
            limit=limit,
            items=[CryptoBacktestHistoryItem(**item) for item in data.get("items", [])],
        )
    except Exception as exc:
        logger.error(f"查询 BTC 历史回测入口失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询 BTC 历史回测入口失败: {str(exc)}"},
        )


@router.get(
    "/crypto/history/{analysis_history_id}",
    response_model=CryptoBacktestHistoryItem,
    responses={
        200: {"description": "BTC 历史分析记录与回测状态"},
        404: {"description": "未找到 BTC 历史分析记录", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取单条 BTC 历史分析记录回测入口",
    description="按 analysis_history_id 返回对应 BTC 分析报告的结构化计划可回测状态和最新计划回测摘要。",
)
def get_crypto_backtest_history_record(
    analysis_history_id: int,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestHistoryItem:
    try:
        service = CryptoBacktestService(db_manager)
        item = service.get_history_record(analysis_history_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "未找到 BTC 历史分析记录"},
            )
        return CryptoBacktestHistoryItem(**item)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询单条 BTC 历史回测入口失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询单条 BTC 历史回测入口失败: {str(exc)}"},
        )


@router.get(
    "/results",
    response_model=BacktestResultsResponse,
    responses={
        200: {"description": "回测结果列表"},
        400: {"description": "请求参数错误", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取回测结果",
    description="分页获取回测结果，支持按股票代码过滤",
)
def get_backtest_results(
    code: Optional[str] = Query(None, description="股票代码筛选"),
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="评估窗口过滤"),
    analysis_date_from: Optional[date] = Query(None, description="分析日期起始（含）"),
    analysis_date_to: Optional[date] = Query(None, description="分析日期结束（含）"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="分析阶段过滤：premarket/intraday/postmarket/unknown"),
    analysis_mode: Optional[BacktestAnalysisModeQuery] = Query(None, description="分析周期过滤：daily/hourly"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=200, description="每页数量"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestResultsResponse:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        data = service.get_recent_evaluations(
            code=code,
            eval_window_days=eval_window_days,
            limit=limit,
            page=page,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
            analysis_mode=analysis_mode,
        )
        items = [BacktestResultItem(**item) for item in data.get("items", [])]
        return BacktestResultsResponse(
            total=int(data.get("total", 0)),
            page=page,
            limit=limit,
            items=items,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询回测结果失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询回测结果失败: {str(exc)}"},
        )


@router.delete(
    "/results/{analysis_history_id}",
    response_model=BacktestDeleteResponse,
    responses={
        200: {"description": "回测结果已删除"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="删除单条回测结果",
    description="删除当前普通回测 engine_version 下指定历史记录与评估窗口的回测结果，并重算汇总",
)
def delete_backtest_result(
    analysis_history_id: int,
    eval_window_days: int = Query(..., ge=1, le=120, description="评估窗口"),
    analysis_mode: BacktestAnalysisModeQuery = Query("daily", description="分析周期：daily/hourly"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestDeleteResponse:
    try:
        service = BacktestService(db_manager)
        result = service.delete_result(
            analysis_history_id=analysis_history_id,
            eval_window_days=eval_window_days,
            analysis_mode=analysis_mode,
        )
        return BacktestDeleteResponse(**result)
    except Exception as exc:
        logger.error(f"删除回测结果失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"删除回测结果失败: {str(exc)}"},
        )


@router.get(
    "/crypto/results",
    response_model=CryptoBacktestResultsResponse,
    responses={
        200: {"description": "BTC 计划回测结果列表"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取 BTC 计划回测结果",
)
def get_crypto_backtest_results(
    code: Optional[str] = Query(None, description="BTC 代码筛选"),
    horizon: Optional[Literal["daily", "intraday"]] = Query(None, description="周期过滤：daily/intraday"),
    plan_type: Optional[CryptoBacktestPlanTypeQuery] = Query(None, description="计划类型过滤"),
    direction: Optional[CryptoBacktestDirectionQuery] = Query(None, description="计划方向过滤：long/short/wait"),
    result_status: Optional[CryptoBacktestResultStatusQuery] = Query(None, description="回测状态过滤"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=200, description="每页数量"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestResultsResponse:
    try:
        service = CryptoBacktestService(db_manager)
        data = service.get_recent_evaluations(
            code=code,
            horizon=horizon,
            plan_type=plan_type,
            direction=direction,
            result_status=result_status,
            page=page,
            limit=limit,
        )
        items = [CryptoBacktestResultItem(**item) for item in data.get("items", [])]
        return CryptoBacktestResultsResponse(
            total=int(data.get("total", 0)),
            page=page,
            limit=limit,
            items=items,
        )
    except Exception as exc:
        logger.error(f"查询 BTC 回测结果失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询 BTC 回测结果失败: {str(exc)}"},
        )


@router.get(
    "/crypto/loss-review",
    response_model=CryptoBacktestLossReviewResponse,
    responses={
        200: {"description": "BTC 亏损回测归因"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取 BTC 亏损复盘",
    description="基于当前回测引擎的成交、费用、止损止盈和指标快照，对净亏损交易给出可追溯归因。",
)
def get_crypto_loss_review(
    code: Optional[str] = Query(None, description="BTC 代码筛选"),
    limit: int = Query(50, ge=1, le=200, description="最多复盘的净亏损记录数"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestLossReviewResponse:
    try:
        service = CryptoBacktestService(db_manager)
        return CryptoBacktestLossReviewResponse(**service.get_loss_review(code=code, limit=limit))
    except Exception as exc:
        logger.error("查询 BTC 亏损复盘失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询 BTC 亏损复盘失败: {str(exc)}"},
        )


@router.delete(
    "/crypto/results/{analysis_history_id}",
    response_model=BacktestDeleteResponse,
    responses={
        200: {"description": "BTC 回测结果已删除"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="删除单条 BTC 计划回测结果",
    description="删除当前 BTC 回测 engine_version 下指定历史记录与计划类型的回测结果，并重算汇总",
)
def delete_crypto_backtest_result(
    analysis_history_id: int,
    plan_type: Literal["daily_long", "daily_short", "intraday"] = Query(..., description="计划类型"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> BacktestDeleteResponse:
    try:
        service = CryptoBacktestService(db_manager)
        result = service.delete_result(
            analysis_history_id=analysis_history_id,
            plan_type=plan_type,
        )
        return BacktestDeleteResponse(**result)
    except Exception as exc:
        logger.error(f"删除 BTC 回测结果失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"删除 BTC 回测结果失败: {str(exc)}"},
        )


@router.get(
    "/crypto/performance",
    response_model=CryptoBacktestMetrics,
    responses={
        200: {"description": "BTC 计划回测表现"},
        404: {"description": "无回测汇总", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取 BTC 计划回测正确率",
)
def get_crypto_backtest_performance(
    scope: Literal["overall", "code", "horizon", "plan_type"] = Query("overall", description="汇总范围"),
    code: Optional[str] = Query(None, description="scope=code 时使用"),
    horizon: Optional[Literal["daily", "intraday"]] = Query(None, description="scope=horizon 时使用"),
    plan_type: Optional[Literal["daily_long", "daily_short", "intraday"]] = Query(None, description="scope=plan_type 时使用"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> CryptoBacktestMetrics:
    try:
        service = CryptoBacktestService(db_manager)
        summary = service.get_summary(
            scope=scope,
            code=code,
            horizon=horizon,
            plan_type=plan_type,
        )
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "未找到 BTC 回测汇总"},
            )
        return CryptoBacktestMetrics(**summary)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询 BTC 回测表现失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询 BTC 回测表现失败: {str(exc)}"},
        )


@router.get(
    "/performance",
    response_model=PerformanceMetrics,
    responses={
        200: {"description": "整体回测表现"},
        400: {"description": "请求参数错误", "model": ErrorResponse},
        404: {"description": "无回测汇总", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取整体回测表现",
)
def get_overall_performance(
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="评估窗口过滤"),
    analysis_date_from: Optional[date] = Query(None, description="分析日期起始（含）"),
    analysis_date_to: Optional[date] = Query(None, description="分析日期结束（含）"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="分析阶段过滤：premarket/intraday/postmarket/unknown"),
    analysis_mode: Optional[BacktestAnalysisModeQuery] = Query(None, description="分析周期过滤：daily/hourly"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> PerformanceMetrics:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        summary = service.get_summary(
            scope="overall",
            code=None,
            eval_window_days=eval_window_days,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
            analysis_mode=analysis_mode,
        )
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "未找到整体回测汇总"},
            )
        return PerformanceMetrics(**summary)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询整体表现失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询整体表现失败: {str(exc)}"},
        )


@router.get(
    "/performance/{code}",
    response_model=PerformanceMetrics,
    responses={
        200: {"description": "单股回测表现"},
        400: {"description": "请求参数错误", "model": ErrorResponse},
        404: {"description": "无回测汇总", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取单股回测表现",
)
def get_stock_performance(
    code: str,
    eval_window_days: Optional[int] = Query(None, ge=1, le=120, description="评估窗口过滤"),
    analysis_date_from: Optional[date] = Query(None, description="分析日期起始（含）"),
    analysis_date_to: Optional[date] = Query(None, description="分析日期结束（含）"),
    analysis_phase: Optional[BacktestAnalysisPhaseQuery] = Query(None, description="分析阶段过滤：premarket/intraday/postmarket/unknown"),
    analysis_mode: Optional[BacktestAnalysisModeQuery] = Query(None, description="分析周期过滤：daily/hourly"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> PerformanceMetrics:
    try:
        _validate_analysis_date_range(analysis_date_from, analysis_date_to)
        service = BacktestService(db_manager)
        summary = service.get_summary(
            scope="stock",
            code=code,
            eval_window_days=eval_window_days,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_phase=analysis_phase,
            analysis_mode=analysis_mode,
        )
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"未找到 {code} 的回测汇总"},
            )
        return PerformanceMetrics(**summary)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_params", "message": str(exc)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询单股表现失败: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询单股表现失败: {str(exc)}"},
        )
