import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backtestApi } from '../../../api/backtest';
import { ReportBacktestSummary } from '../ReportBacktestSummary';

vi.mock('../../../api/backtest', () => ({
  backtestApi: {
    getHistoryRecord: vi.fn(),
  },
}));

const historyRecord = {
  analysisHistoryId: 7,
  code: 'BTC',
  stockName: 'Bitcoin',
  backtestStatus: 'completed',
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
      riskReward: '1:2',
      backtestable: true,
      qualityStatus: 'ok',
      missingFields: [],
      backtestStatus: 'win',
      latestResult: {
        analysisHistoryId: 7,
        code: 'BTCUSDT',
        planType: 'daily_long',
        horizon: 'daily',
        direction: 'long',
        engineVersion: 'btc-plan-v2',
        evalStatus: 'completed',
        entryPrice: 100000,
        entryTriggered: true,
        simulatedReturnPct: 1.25,
        trade: {
          netPnl: 125,
          rMultiple: 1.2,
        },
        execution: {},
        diagnostics: {},
      },
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(backtestApi.getHistoryRecord).mockResolvedValue(historyRecord);
});

describe('ReportBacktestSummary', () => {
  it('renders plan-level backtest status for BTC reports', async () => {
    render(
      <MemoryRouter>
        <ReportBacktestSummary recordId={7} stockCode="BTC" />
      </MemoryRouter>,
    );

    expect(backtestApi.getHistoryRecord).toHaveBeenCalledWith(7);
    expect(await screen.findByText('计划级回测')).toBeInTheDocument();
    expect(screen.getByText('日线多单')).toBeInTheDocument();
    expect(screen.getByText('盈利')).toBeInTheDocument();
    expect(screen.getByText('净收益 1.25%')).toBeInTheDocument();
    expect(screen.getByText('净 PnL 125.00')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /回测页/ })).toHaveAttribute('href', '/backtest');
  });

  it('does not request backtest status for non-BTC reports', async () => {
    const { container } = render(
      <MemoryRouter>
        <ReportBacktestSummary recordId={7} stockCode="600519" />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(backtestApi.getHistoryRecord).not.toHaveBeenCalled();
    });
    expect(container).toBeEmptyDOMElement();
  });
});
