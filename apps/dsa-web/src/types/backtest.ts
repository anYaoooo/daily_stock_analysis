/**
 * Backtest API type definitions
 * Mirrors api/v1/schemas/backtest.py
 */
import type { DecisionAction, MarketPhaseSummary } from './analysis';

// ============ Request / Response ============

export type BacktestAnalysisPhase = 'premarket' | 'intraday' | 'postmarket' | 'unknown';
export type BacktestPhaseFilter = BacktestAnalysisPhase | 'all';
export type BacktestTimeframeFilter = 'all' | 'daily' | 'hourly';

export interface BacktestRunRequest {
  code?: string;
  force?: boolean;
  analysisMode?: 'daily' | 'hourly';
  evalWindowDays?: number;
  minAgeDays?: number;
  limit?: number;
}

export interface BacktestRunResponse {
  processed: number;
  saved: number;
  completed: number;
  insufficient: number;
  errors: number;
}

export interface BacktestDeleteResponse {
  deleted: number;
}

// ============ Result Item ============

export interface BacktestResultItem {
  analysisHistoryId: number;
  code: string;
  stockName?: string;
  analysisDate?: string;
  analysisCreatedAt?: string;
  analysisMode?: string;
  analysisTimeframe?: string;
  evalWindowDays?: number;
  planType?: string;
  horizon?: string;
  direction?: string;
  engineVersion: string;
  evalStatus: string;
  evaluatedAt?: string;
  operationAdvice?: string;
  action?: DecisionAction | null;
  actionLabel?: string | null;
  trendPrediction?: string;
  marketPhase?: string | null;
  marketPhaseSummary?: MarketPhaseSummary | null;
  positionRecommendation?: string;
  startPrice?: number;
  endClose?: number;
  maxHigh?: number;
  minLow?: number;
  stockReturnPct?: number;
  actualReturnPct?: number;
  actualMovement?: string;
  directionExpected?: string;
  directionCorrect?: boolean;
  outcome?: string;
  stopLoss?: number;
  takeProfit?: number;
  hitStopLoss?: boolean;
  hitTakeProfit?: boolean;
  firstHit?: string;
  firstHitDate?: string;
  firstHitTradingDays?: number;
  simulatedEntryPrice?: number;
  simulatedExitPrice?: number;
  simulatedExitReason?: string;
  simulatedReturnPct?: number;
}

export interface CryptoBacktestResultItem {
  analysisHistoryId: number;
  code: string;
  analysisCreatedAt?: string;
  evaluatedAt?: string;
  planType: string;
  horizon: string;
  analysisMode?: string;
  analysisTimeframe?: string;
  direction: string;
  engineVersion: string;
  evalStatus: string;
  evaluationStart?: string;
  evaluationEnd?: string;
  entryPrice?: number;
  stopLoss?: number;
  takeProfit?: number;
  entryTriggered?: boolean;
  entryTriggeredAt?: string;
  directionCorrect?: boolean;
  outcome?: string;
  hitStopLoss?: boolean;
  hitTakeProfit?: boolean;
  firstHit?: string;
  firstHitAt?: string;
  simulatedReturnPct?: number;
  trade: Record<string, unknown>;
  execution: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
}

export interface BacktestResultsResponse {
  total: number;
  page: number;
  limit: number;
  items: BacktestResultItem[];
}

// ============ Performance Metrics ============

export interface PerformanceMetrics {
  scope: string;
  code?: string;
  evalWindowDays?: number;
  horizon?: string;
  analysisMode?: string;
  analysisTimeframe?: string;
  planType?: string;
  engineVersion: string;
  computedAt?: string;

  totalEvaluations: number;
  completedCount: number;
  insufficientCount?: number;
  triggeredCount?: number;
  noEntryCount?: number;
  skippedCount?: number;
  longCount?: number;
  cashCount?: number;
  winCount: number;
  lossCount: number;
  neutralCount: number;

  directionAccuracyPct?: number;
  winRatePct?: number;
  neutralRatePct?: number;
  avgStockReturnPct?: number;
  avgSimulatedReturnPct?: number;

  stopLossTriggerRate?: number;
  takeProfitTriggerRate?: number;
  ambiguousRate?: number;
  avgDaysToFirstHit?: number;

  adviceBreakdown?: Record<string, unknown>;
  planTypeBreakdown?: Record<string, unknown>;
  riskMetrics?: Record<string, unknown>;
  equityCurve?: Array<Record<string, unknown>>;
  diagnostics: Record<string, unknown>;
}

export interface CryptoBacktestMetrics {
  scope: string;
  code?: string;
  horizon?: string;
  analysisMode?: string;
  analysisTimeframe?: string;
  planType?: string;
  engineVersion: string;
  computedAt?: string;
  totalEvaluations: number;
  completedCount: number;
  triggeredCount: number;
  noEntryCount: number;
  skippedCount: number;
  insufficientCount: number;
  winCount: number;
  lossCount: number;
  neutralCount: number;
  directionAccuracyPct?: number;
  winRatePct?: number;
  avgSimulatedReturnPct?: number;
  planTypeBreakdown: Record<string, unknown>;
  riskMetrics: Record<string, unknown>;
  equityCurve: Array<Record<string, unknown>>;
  diagnostics: Record<string, unknown>;
}
