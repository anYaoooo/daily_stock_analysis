/**
 * Backtest API type definitions
 * Mirrors api/v1/schemas/backtest.py
 */
import type { DecisionAction, MarketPhaseSummary } from './analysis';

// ============ Request / Response ============

export type BacktestAnalysisPhase = 'premarket' | 'intraday' | 'postmarket' | 'unknown';
export type BacktestPhaseFilter = BacktestAnalysisPhase | 'all';
export type BacktestTimeframeFilter = 'all' | 'daily' | 'hourly';
export type CryptoBacktestDirectionFilter = 'all' | 'long' | 'short' | 'wait';
export type CryptoBacktestPlanTypeFilter = 'all' | 'daily_long' | 'daily_short' | 'intraday';
export type CryptoBacktestResultStatusFilter =
  | 'all'
  | 'pending'
  | 'win'
  | 'loss'
  | 'neutral'
  | 'no_entry'
  | 'skipped'
  | 'insufficient_data'
  | 'invalid_plan';

export interface BacktestRunRequest {
  code?: string;
  force?: boolean;
  analysisMode?: 'daily' | 'hourly';
  evalWindowDays?: number;
  minAgeDays?: number;
  limit?: number;
}

export interface CryptoBacktestSelectedRunRequest {
  analysisHistoryIds: number[];
  planTypes?: string[];
  force?: boolean;
}

export interface CryptoBacktestTaskAccepted {
  taskId: string;
  status: 'pending' | 'processing';
  message?: string;
}

export interface CryptoBacktestTaskStatus extends Omit<CryptoBacktestTaskAccepted, 'status'> {
  progress: number;
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled';
  result?: BacktestRunResponse;
  error?: string;
}

export interface BacktestRunResponse {
  processed: number;
  saved: number;
  completed: number;
  insufficient: number;
  errors: number;
  skipped?: number;
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
  signalTriggered?: boolean;
  signalTriggeredAt?: string;
  orderStatus?: string;
  orderRejectionReason?: string;
  entryTriggered?: boolean;
  entryTriggeredAt?: string;
  mfePct?: number;
  maePct?: number;
  directionCorrectRaw?: boolean;
  directionCorrect?: boolean;
  outcome?: string;
  hitStopLoss?: boolean;
  hitTakeProfit?: boolean;
  firstHit?: string;
  firstHitAt?: string;
  simulatedExitReason?: string;
  simulatedReturnPct?: number;
  missedFavorableMovePct?: number;
  missedAdverseMovePct?: number;
  trade: Record<string, unknown>;
  execution: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
}

export interface CryptoBacktestHistoryPlan {
  planType: string;
  horizon: string;
  setupType?: 'breakout' | 'pullback' | string;
  analysisMode?: string;
  analysisTimeframe?: string;
  direction: string;
  entryPrice?: number | null;
  stopLoss?: number | null;
  takeProfit?: number | null;
  executionContract?: Record<string, unknown> | null;
  invalidCondition?: string | null;
  riskReward?: string | null;
  positionHint?: string | null;
  confidence?: string | null;
  tradeabilityStatus?: string | null;
  tradeabilityReasons?: string[];
  positionMultiplierCap?: number | null;
  countertrendControl?: Record<string, unknown> | null;
  planQuality?: Record<string, unknown> | null;
  backtestable: boolean;
  qualityStatus: string;
  missingFields: string[];
  noTradeReason?: string | null;
  backtestStatus: string;
  latestResult?: CryptoBacktestResultItem | null;
  indicatorTags?: Record<string, unknown> | null;
}

export interface CryptoBacktestHistoryItem {
  analysisHistoryId: number;
  queryId?: string;
  code: string;
  stockName?: string;
  reportType?: string;
  analysisCreatedAt?: string;
  analysisMode?: string;
  analysisTimeframe?: string;
  analysisSummary?: string;
  operationAdvice?: string;
  trendPrediction?: string;
  backtestStatus: string;
  plans: CryptoBacktestHistoryPlan[];
  diagnostics?: Record<string, unknown> & { backtestStatusReason?: string | null };
}

export interface BacktestResultsResponse {
  total: number;
  page: number;
  limit: number;
  items: BacktestResultItem[];
}

export interface CryptoBacktestLossReviewItem {
  analysisHistoryId: number;
  code: string;
  planType: string;
  horizon: string;
  direction: string;
  analysisCreatedAt?: string;
  simulatedReturnPct?: number;
  netPnl?: number;
  primaryCause: string;
  causeGroup: string;
  confidence: string;
  title: string;
  explanation: string;
  evidence: string[];
  improvement: string;
  externalContext: string;
  indicatorTags: Record<string, unknown>;
}

export interface CryptoBacktestLossReviewResponse {
  engineVersion: string;
  reviewedResults: number;
  lossCount: number;
  causeBreakdown: Record<string, number>;
  indicatorPatterns: Array<{
    dimension: string;
    key: string;
    lossCount: number;
    note: string;
  }>;
  improvementSuggestions: string[];
  items: CryptoBacktestLossReviewItem[];
}

export interface CryptoBacktestHistoryResponse {
  total: number;
  page: number;
  limit: number;
  items: CryptoBacktestHistoryItem[];
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
  directionAccuracyRawPct?: number;
  signalQualityRatePct?: number;
  executionFillRatePct?: number;
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
  directionAccuracyRawPct?: number;
  signalQualityRatePct?: number;
  executionFillRatePct?: number;
  winRatePct?: number;
  avgSimulatedReturnPct?: number;
  planTypeBreakdown: Record<string, unknown>;
  riskMetrics: Record<string, unknown>;
  equityCurve: Array<Record<string, unknown>>;
  diagnostics: Record<string, unknown>;
}
