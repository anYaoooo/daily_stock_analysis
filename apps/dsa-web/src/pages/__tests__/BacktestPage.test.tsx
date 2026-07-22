import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import BacktestPage from '../BacktestPage';

const {
  mockGetHistory,
  mockGetLossReview,
  mockGetOverallPerformance,
  mockRunSelected,
  mockDeleteRecords,
} = vi.hoisted(() => ({
  mockGetHistory: vi.fn(),
  mockGetLossReview: vi.fn(),
  mockGetOverallPerformance: vi.fn(),
  mockRunSelected: vi.fn(),
  mockDeleteRecords: vi.fn(),
}));

vi.mock('../../api/backtest', () => ({
  backtestApi: {
    getHistory: mockGetHistory,
    getLossReview: mockGetLossReview,
    getOverallPerformance: mockGetOverallPerformance,
    runSelected: mockRunSelected,
  },
}));

vi.mock('../../api/history', () => ({
  historyApi: {
    deleteRecords: mockDeleteRecords,
  },
}));

const basePerformance = {
  scope: 'overall',
  engineVersion: 'btc-plan-v3',
  totalEvaluations: 3,
  completedCount: 3,
  triggeredCount: 2,
  noEntryCount: 1,
  skippedCount: 0,
  insufficientCount: 0,
  winCount: 1,
  lossCount: 1,
  neutralCount: 0,
  directionAccuracyPct: 50,
  winRatePct: 50,
  avgSimulatedReturnPct: 0.4,
  planTypeBreakdown: {},
  riskMetrics: {},
  equityCurve: [],
  diagnostics: {
    metricSemantics: 'structured_execution_contract',
    signalTriggeredCount: 3,
    rejectedOrderCount: 1,
    orderFillRatePct: 66.67,
    avgMissedFavorableMovePct: 1.25,
    rawTriggeredCount: 3,
    overlapExcludedCount: 1,
    sampleConfidence: { isLowConfidence: true, sampleCount: 2, minimumSampleCount: 100 },
    indicatorGroupBreakdown: {
      groups: {
        'price_action.state': [
          {
            dimension: 'price_action.state',
            dimensionLabel: '价格行为',
            key: 'breakout',
            totalEvaluations: 2,
            triggeredCount: 1,
            winRatePct: 100,
            avgSimulatedReturnPct: 1.2,
            maxDrawdownPct: 0,
            avgRMultiple: 1.1,
            sampleConfidence: { isLowConfidence: true },
          },
        ],
      },
    },
  },
};

const baseLossReview = {
  engineVersion: 'btc-plan-v5',
  reviewedResults: 1,
  lossCount: 1,
  causeBreakdown: { direction_mismatch: 1 },
  indicatorPatterns: [{
    dimension: 'volume',
    key: 'low',
    lossCount: 1,
    note: '仅表示亏损样本中的共同特征，不代表已证明的因果关系。',
  }],
  improvementSuggestions: ['按指标组合与周期拆分亏损样本，验证量能、多周期方向和关键位确认是否需要收紧。'],
  items: [{
    analysisHistoryId: 7,
    code: 'BTCUSDT',
    planType: 'daily_long',
    horizon: 'daily',
    direction: 'long',
    simulatedReturnPct: -1.2,
    netPnl: -12,
    primaryCause: 'direction_mismatch',
    causeGroup: 'methodology',
    confidence: 'medium',
    title: '方向判断与后续走势不一致',
    explanation: '回测窗口内的实际价格方向没有支持该交易计划。',
    evidence: ['计划方向：long'],
    improvement: '提高量能和多周期方向确认门槛。',
    externalContext: '没有直接的外部事件证据。',
    indicatorTags: {},
  }],
};

const baseHistoryItem = {
  analysisHistoryId: 7,
  queryId: 'q-7',
  code: 'BTCUSDT',
  stockName: 'Bitcoin',
  reportType: 'stock',
  analysisCreatedAt: '2026-06-25T08:00:00',
  analysisMode: 'daily',
  analysisTimeframe: '日线',
  analysisSummary: '突破后等待回踩确认',
  operationAdvice: '观望',
  trendPrediction: '震荡偏多',
  backtestStatus: 'pending',
  plans: [
    {
      planType: 'daily_long',
      horizon: 'daily',
      setupType: 'pullback',
      analysisMode: 'daily',
      analysisTimeframe: '日线',
      direction: 'long',
      entryPrice: 100000,
      stopLoss: 99000,
      takeProfit: 102000,
      executionContract: {
        version: 'btc-execution-v1',
      },
      invalidCondition: '跌回区间',
      riskReward: '1:2',
      positionHint: '0.5% 风险',
      confidence: '中',
      backtestable: true,
      qualityStatus: 'ok',
      missingFields: [],
      noTradeReason: null,
      backtestStatus: 'pending',
      latestResult: null,
      indicatorTags: {
        priceAction: { state: 'breakout' },
        ema: { structure: 'bullish' },
        vwap: { pricePosition: 'above' },
        volume: { confirmation: 'high' },
        intraday: { alignment: 'aligned_long' },
        event: { type: 'none' },
      },
    },
  ],
};

function renderPage() {
  render(
    <MemoryRouter>
      <BacktestPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetHistory.mockResolvedValue({
    total: 1,
    page: 1,
    limit: 20,
    items: [baseHistoryItem],
  });
  mockGetOverallPerformance.mockResolvedValue(basePerformance);
  mockGetLossReview.mockResolvedValue(baseLossReview);
  mockRunSelected.mockResolvedValue({
    processed: 1,
    saved: 1,
    completed: 1,
    insufficient: 0,
    skipped: 0,
    errors: 0,
  });
  mockDeleteRecords.mockResolvedValue({ deleted: 1 });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

describe('BacktestPage', () => {
  it('loads BTC history records in a paginated table and opens plan details', async () => {
    renderPage();

    expect(await screen.findByText('BTCUSDT')).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'BTC 回测历史分析记录' })).toBeInTheDocument();
    expect(screen.getByText('突破后等待回踩确认')).toBeInTheDocument();
    expect(screen.getAllByText('日线多单').length).toBeGreaterThan(0);
    expect(screen.getByText('指标分组复盘')).toBeInTheDocument();
    expect(screen.getByText('价格行为 · breakout')).toBeInTheDocument();
    expect(screen.getByText('策略契约胜率')).toBeInTheDocument();
    expect(screen.getByText('亏损复盘')).toBeInTheDocument();
    expect(screen.getByText('方向判断与后续走势不一致')).toBeInTheDocument();
    expect(screen.getByText('独立成交 / 已完成评估')).toBeInTheDocument();
    expect(screen.getByText('信号 / 成交 / 拒单')).toBeInTheDocument();
    expect(screen.getByText('信号成交率')).toBeInTheDocument();
    expect(screen.getByText('拒单后平均有利波动')).toBeInTheDocument();
    expect(screen.getByText('不可评估 / 等待数据')).toBeInTheDocument();
    expect(screen.getByText('原始触发 / 重叠排除')).toBeInTheDocument();
    expect(mockGetHistory).toHaveBeenCalledWith({
      code: 'BTC',
      analysisMode: 'all',
      direction: 'all',
      planType: 'all',
      resultStatus: 'all',
      page: 1,
      limit: 20,
    });
    expect(mockGetLossReview).toHaveBeenCalledWith({ code: 'BTC' });

    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('回踩')).toBeInTheDocument();
    expect(screen.getByText('PA breakout')).toBeInTheDocument();
    expect(screen.getByText('结构化执行契约')).toBeInTheDocument();
  });

  it('shows a triggered signal separately from a rejected fill', async () => {
    mockGetHistory.mockResolvedValueOnce({
      total: 1,
      page: 1,
      limit: 20,
      items: [{
        ...baseHistoryItem,
        backtestStatus: 'completed',
        plans: [{
          ...baseHistoryItem.plans[0],
          backtestStatus: 'signal_rejected',
          latestResult: {
            analysisHistoryId: 7,
            code: 'BTCUSDT',
            planType: 'daily_long',
            horizon: 'daily',
            direction: 'long',
            engineVersion: 'btc-plan-v5',
            evalStatus: 'completed',
            outcome: 'no_entry',
            signalTriggered: true,
            orderStatus: 'rejected',
            orderRejectionReason: 'risk_reward_below_minimum',
            entryTriggered: false,
            simulatedExitReason: 'fill_quality_gate_rejected',
            missedFavorableMovePct: 1.5,
            missedAdverseMovePct: 0.4,
            trade: {},
            execution: {},
            diagnostics: {},
          },
        }],
      }],
    });

    renderPage();
    expect(await screen.findByText('信号触发后拒单 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    expect(await screen.findByText('信号 已触发')).toBeInTheDocument();
    expect(screen.getByText('委托 已拒单')).toBeInTheDocument();
    expect(screen.getByText(/实际成交价未通过风控/)).toBeInTheDocument();
  });

  it('keeps core backtest data available when loss review loading fails', async () => {
    mockGetLossReview.mockRejectedValueOnce(new Error('loss review unavailable'));

    renderPage();

    expect(await screen.findByText('BTCUSDT')).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'BTC 回测历史分析记录' })).toBeInTheDocument();
    expect(screen.queryByText('亏损复盘')).not.toBeInTheDocument();
  });

  it('sends analysis mode, direction, plan type, and result status filters', async () => {
    renderPage();
    await screen.findByText('BTCUSDT');

    fireEvent.change(screen.getByLabelText('分析模式'), { target: { value: 'hourly' } });
    fireEvent.change(screen.getByLabelText('方向'), { target: { value: 'short' } });
    fireEvent.change(screen.getByLabelText('计划类型'), { target: { value: 'intraday' } });
    fireEvent.change(screen.getByLabelText('结果状态'), { target: { value: 'loss' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => {
      expect(mockGetHistory).toHaveBeenLastCalledWith({
        code: 'BTC',
        analysisMode: 'hourly',
        direction: 'short',
        planType: 'intraday',
        resultStatus: 'loss',
        page: 1,
        limit: 20,
      });
    });
  });

  it('runs one selected plan through the history-id API', async () => {
    renderPage();
    await screen.findByText('BTCUSDT');

    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    fireEvent.click(await screen.findByRole('button', { name: '回测计划' }));

    await waitFor(() => {
      expect(mockRunSelected).toHaveBeenCalledWith({
        analysisHistoryIds: [7],
        planTypes: ['daily_long'],
        force: false,
      });
    });
    expect(await screen.findByText('写入')).toBeInTheDocument();
  });

  it('labels explicit wait plans as excluded samples instead of missing fields', async () => {
    mockGetHistory.mockResolvedValue({
      total: 1,
      page: 1,
      limit: 20,
      items: [{
        ...baseHistoryItem,
        analysisMode: 'hourly',
        plans: [{
          ...baseHistoryItem.plans[0],
          planType: 'intraday',
          horizon: 'intraday',
          direction: 'wait',
          executionContract: null,
          backtestable: false,
          qualityStatus: 'no_trade_plan',
          missingFields: [],
          noTradeReason: '小时线结构尚未确认',
          backtestStatus: 'skipped',
        }],
      }],
    });

    renderPage();
    await screen.findByText('BTCUSDT');
    fireEvent.click(screen.getByRole('button', { name: '详情' }));

    expect(await screen.findByText('观望计划，不计入有效样本：小时线结构尚未确认')).toBeInTheDocument();
    expect(screen.queryByText(/缺少关键字段/)).not.toBeInTheDocument();
    expect(screen.getAllByText('不计入样本').length).toBeGreaterThan(0);
  });

  it('batch-runs selected history records and deletes with traceability warning', async () => {
    renderPage();
    await screen.findByText('BTCUSDT');

    fireEvent.click(screen.getByRole('checkbox', { name: '选择记录 #7' }));
    fireEvent.click(screen.getByRole('button', { name: /批量回测/ }));

    await waitFor(() => {
      expect(mockRunSelected).toHaveBeenCalledWith({
        analysisHistoryIds: [7],
        planTypes: undefined,
        force: false,
      });
    });

    fireEvent.click(screen.getByRole('button', { name: '批量删除' }));
    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith('确认删除 1 条历史分析记录？对应报告入口和回测追溯也会受影响。');
      expect(mockDeleteRecords).toHaveBeenCalledWith([7]);
    });
  });
});
