import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  BacktestRunRequest,
  BacktestRunResponse,
  BacktestDeleteResponse,
  BacktestResultsResponse,
  BacktestResultItem,
  PerformanceMetrics,
  BacktestPhaseFilter,
  BacktestTimeframeFilter,
  CryptoBacktestDirectionFilter,
  CryptoBacktestHistoryResponse,
  CryptoBacktestHistoryItem,
  CryptoBacktestPlanTypeFilter,
  CryptoBacktestResultStatusFilter,
  CryptoBacktestSelectedRunRequest,
} from '../types/backtest';

// ============ API ============

export const backtestApi = {
  /**
   * Trigger backtest evaluation
   */
  run: async (params: BacktestRunRequest = {}): Promise<BacktestRunResponse> => {
    const requestData: Record<string, unknown> = {};
    if (params.code) requestData.code = params.code;
    if (params.force) requestData.force = params.force;
    if (params.analysisMode) requestData.analysis_mode = params.analysisMode;
    if (params.evalWindowDays) requestData.eval_window_days = params.evalWindowDays;
    if (params.minAgeDays != null) requestData.min_age_days = params.minAgeDays;
    if (params.limit) requestData.limit = params.limit;

    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/backtest/crypto/run',
      requestData,
    );
    return toCamelCase<BacktestRunResponse>(response.data);
  },

  /**
   * Trigger BTC backtests for selected analysis history records
   */
  runSelected: async (params: CryptoBacktestSelectedRunRequest): Promise<BacktestRunResponse> => {
    const requestData: Record<string, unknown> = {
      analysis_history_ids: params.analysisHistoryIds,
    };
    if (params.planTypes?.length) requestData.plan_types = params.planTypes;
    if (params.force) requestData.force = params.force;

    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/backtest/crypto/run-selected',
      requestData,
    );
    return toCamelCase<BacktestRunResponse>(response.data);
  },

  /**
   * Get BTC analysis history records with plan-level backtest status
   */
  getHistory: async (params: {
    code?: string;
    analysisMode?: BacktestTimeframeFilter;
    direction?: CryptoBacktestDirectionFilter;
    planType?: CryptoBacktestPlanTypeFilter;
    resultStatus?: CryptoBacktestResultStatusFilter;
    page?: number;
    limit?: number;
  } = {}): Promise<CryptoBacktestHistoryResponse> => {
    const { code, page = 1, limit = 20 } = params;
    const queryParams: Record<string, string | number> = { page, limit };
    if (code) queryParams.code = code;
    if (params.analysisMode && params.analysisMode !== 'all') queryParams.analysis_mode = params.analysisMode;
    if (params.direction && params.direction !== 'all') queryParams.direction = params.direction;
    if (params.planType && params.planType !== 'all') queryParams.plan_type = params.planType;
    if (params.resultStatus && params.resultStatus !== 'all') queryParams.result_status = params.resultStatus;

    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/backtest/crypto/history',
      { params: queryParams },
    );
    const data = toCamelCase<CryptoBacktestHistoryResponse>(response.data);
    return {
      total: data.total,
      page: data.page,
      limit: data.limit,
      items: (data.items || []).map(item => toCamelCase<CryptoBacktestHistoryItem>(item)),
    };
  },

  /**
   * Get one BTC analysis history record with plan-level backtest status
   */
  getHistoryRecord: async (analysisHistoryId: number): Promise<CryptoBacktestHistoryItem> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/backtest/crypto/history/${analysisHistoryId}`,
    );
    return toCamelCase<CryptoBacktestHistoryItem>(response.data);
  },

  /**
   * Delete one backtest result for the current engine version
   */
  deleteResult: async (
    analysisHistoryId: number,
    evalWindowDays?: number,
    planType?: string,
    analysisMode?: 'daily' | 'hourly',
  ): Promise<BacktestDeleteResponse> => {
    const params: Record<string, string | number> = {};
    if (planType) {
      params.plan_type = planType;
    } else if (evalWindowDays != null) {
      params.eval_window_days = evalWindowDays;
      if (analysisMode) params.analysis_mode = analysisMode;
    }
    const response = await apiClient.delete<Record<string, unknown>>(
      planType
        ? `/api/v1/backtest/crypto/results/${analysisHistoryId}`
        : `/api/v1/backtest/results/${analysisHistoryId}`,
      { params },
    );
    return toCamelCase<BacktestDeleteResponse>(response.data);
  },

  /**
   * Get paginated backtest results
   */
  getResults: async (params: {
    code?: string;
    evalWindowDays?: number;
    analysisDateFrom?: string;
    analysisDateTo?: string;
    analysisPhase?: BacktestPhaseFilter;
    analysisMode?: BacktestTimeframeFilter;
    direction?: CryptoBacktestDirectionFilter;
    planType?: CryptoBacktestPlanTypeFilter;
    resultStatus?: CryptoBacktestResultStatusFilter;
    page?: number;
    limit?: number;
  } = {}): Promise<BacktestResultsResponse> => {
    const { code, page = 1, limit = 20 } = params;

    const queryParams: Record<string, string | number> = { page, limit };
    if (code) queryParams.code = code;
    if (params.analysisMode && params.analysisMode !== 'all') {
      queryParams.horizon = params.analysisMode === 'hourly' ? 'intraday' : 'daily';
    }
    if (params.direction && params.direction !== 'all') queryParams.direction = params.direction;
    if (params.planType && params.planType !== 'all') queryParams.plan_type = params.planType;
    if (params.resultStatus && params.resultStatus !== 'all') queryParams.result_status = params.resultStatus;

    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/backtest/crypto/results',
      { params: queryParams },
    );

    const data = toCamelCase<BacktestResultsResponse>(response.data);
    return {
      total: data.total,
      page: data.page,
      limit: data.limit,
      items: (data.items || []).map(item => toCamelCase<BacktestResultItem>(item)),
    };
  },

  /**
   * Get overall performance metrics
   */
  getOverallPerformance: async (params: {
    evalWindowDays?: number;
    analysisDateFrom?: string;
    analysisDateTo?: string;
    analysisPhase?: BacktestPhaseFilter;
    analysisMode?: BacktestTimeframeFilter;
    planType?: CryptoBacktestPlanTypeFilter;
  } = {}): Promise<PerformanceMetrics | null> => {
    try {
      const queryParams: Record<string, string | number> = {};
      if (params.analysisMode && params.analysisMode !== 'all') queryParams.horizon = params.analysisMode === 'hourly' ? 'intraday' : 'daily';
      if (params.planType && params.planType !== 'all') {
        queryParams.scope = 'plan_type';
        queryParams.plan_type = params.planType;
      }
      const response = await apiClient.get<Record<string, unknown>>(
        '/api/v1/backtest/crypto/performance',
        { params: queryParams },
      );
      return toCamelCase<PerformanceMetrics>(response.data);
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { status?: number } };
        if (axiosErr.response?.status === 404) return null;
      }
      throw err;
    }
  },

  /**
   * Get per-stock performance metrics
   */
  getStockPerformance: async (code: string): Promise<PerformanceMetrics | null> => {
    try {
      const queryParams: Record<string, string | number> = {};
      queryParams.scope = 'code';
      queryParams.code = code;
      const response = await apiClient.get<Record<string, unknown>>(
        '/api/v1/backtest/crypto/performance',
        { params: queryParams },
      );
      return toCamelCase<PerformanceMetrics>(response.data);
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { status?: number } };
        if (axiosErr.response?.status === 404) return null;
      }
      throw err;
    }
  },
};
