import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import BacktestPage from '../BacktestPage';

const {
  mockGetHistory,
  mockGetOverallPerformance,
  mockRunSelected,
  mockDeleteRecords,
} = vi.hoisted(() => ({
  mockGetHistory: vi.fn(),
  mockGetOverallPerformance: vi.fn(),
  mockRunSelected: vi.fn(),
  mockDeleteRecords: vi.fn(),
}));

vi.mock('../../api/backtest', () => ({
  backtestApi: {
    getHistory: mockGetHistory,
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
  engineVersion: 'btc-plan-v2',
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
      analysisMode: 'daily',
      analysisTimeframe: '日线',
      direction: 'long',
      entryPrice: 100000,
      stopLoss: 99000,
      takeProfit: 102000,
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
  it('loads BTC history records with plan summaries and indicator grouping', async () => {
    renderPage();

    expect(await screen.findByText('BTCUSDT')).toBeInTheDocument();
    expect(screen.getByText('突破后等待回踩确认')).toBeInTheDocument();
    expect(screen.getAllByText('日线多单').length).toBeGreaterThan(0);
    expect(screen.getByText('PA breakout')).toBeInTheDocument();
    expect(screen.getByText('指标分组复盘')).toBeInTheDocument();
    expect(screen.getByText('价格行为 · breakout')).toBeInTheDocument();
    expect(mockGetHistory).toHaveBeenCalledWith({
      code: 'BTC',
      analysisMode: 'all',
      direction: 'all',
      planType: 'all',
      resultStatus: 'all',
      page: 1,
      limit: 20,
    });
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

    fireEvent.click(screen.getByRole('button', { name: '回测计划' }));

    await waitFor(() => {
      expect(mockRunSelected).toHaveBeenCalledWith({
        analysisHistoryIds: [7],
        planTypes: ['daily_long'],
        force: false,
      });
    });
    expect(await screen.findByText('写入')).toBeInTheDocument();
  });

  it('batch-runs selected history records and deletes with traceability warning', async () => {
    renderPage();
    await screen.findByText('BTCUSDT');

    fireEvent.click(screen.getByRole('checkbox'));
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
