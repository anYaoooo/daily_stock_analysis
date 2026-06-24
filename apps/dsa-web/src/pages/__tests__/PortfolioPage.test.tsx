import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';
import PortfolioPage from '../PortfolioPage';

const {
  cancelOrder,
  createOrder,
  getBalance,
  getOpenOrders,
  getPositions,
  getStatus,
  setLeverage,
  setMarginMode,
} = vi.hoisted(() => ({
  cancelOrder: vi.fn(),
  createOrder: vi.fn(),
  getBalance: vi.fn(),
  getOpenOrders: vi.fn(),
  getPositions: vi.fn(),
  getStatus: vi.fn(),
  setLeverage: vi.fn(),
  setMarginMode: vi.fn(),
}));

vi.mock('../../api/cryptoTrading', () => ({
  cryptoTradingApi: {
    getStatus,
    getBalance,
    getPositions,
    getOpenOrders,
    createOrder,
    cancelOrder,
    setLeverage,
    setMarginMode,
  },
}));

function renderPage() {
  return render(
    <UiLanguageProvider>
      <PortfolioPage />
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  getStatus.mockResolvedValue({
    exchange: 'okx',
    configured: true,
    tradingEnabled: false,
    dryRun: true,
    sandbox: false,
    demoTrading: false,
    defaultSymbol: 'BTC/USDT:USDT',
    defaultType: 'swap',
    tdMode: 'cross',
    settle: null,
  });
  getBalance.mockResolvedValue({
    exchange: 'okx',
    currency: 'USDT',
    free: 100,
    used: 5,
    total: 105,
  });
  getPositions.mockResolvedValue({
    exchange: 'okx',
    items: [{ symbol: 'BTC/USDT:USDT', side: 'long', contracts: '0.01', entryPrice: '60000', markPrice: '61000', unrealizedPnl: '10' }],
  });
  getOpenOrders.mockResolvedValue({
    exchange: 'okx',
    items: [{ id: 'ord-1', symbol: 'BTC/USDT:USDT', side: 'buy', amount: '0.01', price: '59000', status: 'open' }],
  });
  createOrder.mockResolvedValue({
    dryRun: true,
    order: { symbol: 'BTC/USDT:USDT', side: 'buy', amount: 0.01 },
  });
  cancelOrder.mockResolvedValue({
    dryRun: true,
    cancel: { orderId: 'ord-1', symbol: 'BTC/USDT:USDT' },
  });
  setLeverage.mockResolvedValue({
    dryRun: true,
    leverage: { leverage: 3, symbol: 'BTC/USDT:USDT' },
  });
  setMarginMode.mockResolvedValue({
    dryRun: true,
    marginMode: { marginMode: 'cross', symbol: 'BTC/USDT:USDT' },
  });
});

describe('PortfolioPage BTC trading workspace', () => {
  it('loads exchange status, USDT balance, positions, and open orders', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'BTC 交易' })).toBeInTheDocument();
    expect(await screen.findByText('Key 已配置')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getAllByText('BTC/USDT:USDT').length).toBeGreaterThan(0);
    expect(getBalance).toHaveBeenCalledWith('USDT');
    expect(getPositions).toHaveBeenCalledWith('BTC');
    expect(getOpenOrders).toHaveBeenCalledWith('BTC');
  });

  it('keeps read calls disabled when exchange keys are missing but still renders the dry-run ticket', async () => {
    getStatus.mockResolvedValueOnce({
      exchange: 'okx',
      configured: false,
      tradingEnabled: false,
      dryRun: true,
      sandbox: false,
      demoTrading: false,
      defaultSymbol: 'BTC/USDT:USDT',
      defaultType: 'swap',
      tdMode: 'cross',
      settle: null,
    });

    renderPage();

    expect(await screen.findByText('交易所私有 API 未配置')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '交易下单' })).toBeInTheDocument();
    expect(getBalance).not.toHaveBeenCalled();
    expect(getPositions).not.toHaveBeenCalled();
    expect(getOpenOrders).not.toHaveBeenCalled();
  });

  it('submits a dry-run BTC long order through the crypto trading API', async () => {
    renderPage();

    await screen.findByRole('heading', { name: '交易下单' });
    fireEvent.change(screen.getByLabelText('价格'), { target: { value: '60000' } });
    fireEvent.click(screen.getByRole('button', { name: '提交订单' }));

    await waitFor(() => expect(createOrder).toHaveBeenCalledWith(expect.objectContaining({
      symbol: 'BTC/USDT:USDT',
      orderType: 'limit',
      side: 'buy',
      amount: 0.01,
      price: 60000,
      tdMode: 'cross',
      posSide: 'long',
    })));
    expect(await screen.findByText('订单预览')).toBeInTheDocument();
  });

  it('submits cancel, leverage, and margin actions', async () => {
    renderPage();

    await screen.findByRole('heading', { name: '撤单' });
    fireEvent.change(screen.getByLabelText('订单 ID'), { target: { value: 'ord-1' } });
    fireEvent.click(screen.getByRole('button', { name: '撤销订单' }));
    await waitFor(() => expect(cancelOrder).toHaveBeenCalledWith({ orderId: 'ord-1', symbol: 'BTC/USDT:USDT' }));

    fireEvent.click(screen.getByRole('button', { name: '设置杠杆' }));
    await waitFor(() => expect(setLeverage).toHaveBeenCalledWith(expect.objectContaining({ leverage: 3, marginMode: 'cross' })));

    fireEvent.click(screen.getByRole('button', { name: '设置保证金' }));
    await waitFor(() => expect(setMarginMode).toHaveBeenCalledWith({ marginMode: 'cross', symbol: 'BTC/USDT:USDT' }));
  });
});
