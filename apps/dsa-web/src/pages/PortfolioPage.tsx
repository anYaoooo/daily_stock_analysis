import { useCallback, useEffect, useMemo, useState } from 'react';
import type React from 'react';
import { ArrowDownToLine, ArrowUpFromLine, RefreshCw, ShieldCheck, SlidersHorizontal, WalletCards, XCircle } from 'lucide-react';
import { cryptoTradingApi } from '../api/cryptoTrading';
import { AppPage } from '../components/common/AppPage';
import { InlineAlert } from '../components/common/InlineAlert';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type {
  CryptoBalanceResponse,
  CryptoListResponse,
  CryptoMarginMode,
  CryptoOrderSide,
  CryptoOrderType,
  CryptoPositionSide,
  CryptoTradingStatusResponse,
  CryptoTradeMode,
} from '../types/cryptoTrading';
import { cn } from '../utils/cn';

type Feedback = {
  tone: 'success' | 'warning' | 'danger' | 'info';
  title: string;
  message: string;
};

type TradingActionResult = {
  dryRun: boolean;
};

type OrderForm = {
  symbol: string;
  orderType: CryptoOrderType;
  side: CryptoOrderSide;
  amount: string;
  price: string;
  tdMode: CryptoTradeMode;
  posSide: CryptoPositionSide;
  reduceOnly: boolean;
  clientOrderId: string;
};

const DEFAULT_SYMBOL = 'BTC/USDT:USDT';

const text = {
  zh: {
    eyebrow: 'CCXT 交易',
    title: 'BTC 交易',
    description: '查看 CCXT 交易所账户状态、BTC 合约持仓与挂单，并手动提交 BTC 多空交易指令。',
    refresh: '刷新',
    refreshing: '刷新中',
    status: '接口状态',
    configured: 'Key 已配置',
    notConfigured: 'Key 未配置',
    dryRun: 'Dry-run',
    live: '真实交易',
    sandbox: 'Sandbox',
    demoTrading: 'Demo Trading',
    production: '生产环境',
    defaultContract: '默认合约',
    tradeMode: '交易模式',
    safetyTitle: '交易保护',
    safetyDryRun: '当前写操作只返回预览，不会请求交易所写接口。',
    safetyLive: '当前允许真实交易，请提交前确认方向、数量、价格和仓位方向。',
    configWarningTitle: '交易所私有 API 未配置',
    configWarning: '余额、持仓和挂单需要配置当前交易所 API Key；OKX 需要 OKX_API_KEY / OKX_SECRET / OKX_PASSWORD，Bybit Demo 需要 BYBIT_API_KEY / BYBIT_SECRET / BYBIT_DEMO_TRADING=true；dry-run 下单预览仍可使用。',
    balance: 'USDT 余额',
    free: '可用',
    used: '占用',
    total: '总额',
    positions: 'BTC 持仓',
    openOrders: '当前挂单',
    noPositions: '暂无 BTC 持仓',
    noOrders: '暂无挂单',
    orderTicket: '交易下单',
    symbol: '合约',
    orderType: '订单类型',
    market: '市价',
    limit: '限价',
    direction: '方向',
    buyLong: '买入 / 开多',
    sellShort: '卖出 / 开空',
    amount: '数量',
    price: '价格',
    posSide: '仓位方向',
    reduceOnly: '只减仓',
    clientOrderId: '客户端订单号',
    submitOrder: '提交订单',
    submitting: '提交中',
    leveragePanel: '杠杆与保证金',
    leverage: '杠杆倍数',
    marginMode: '保证金模式',
    setLeverage: '设置杠杆',
    setMarginMode: '设置保证金',
    cancelPanel: '撤单',
    orderId: '订单 ID',
    cancelOrder: '撤销订单',
    actionResult: '操作结果',
    loadFailed: '交易数据加载失败',
    orderSuccess: '订单已提交',
    orderPreview: '订单预览',
    cancelSuccess: '撤单已提交',
    cancelPreview: '撤单预览',
    leverageSuccess: '杠杆设置已提交',
    leveragePreview: '杠杆设置预览',
    marginSuccess: '保证金设置已提交',
    marginPreview: '保证金设置预览',
    validationAmount: '请输入大于 0 的数量。',
    validationPrice: '限价单请输入大于 0 的价格。',
    validationLeverage: '请输入大于 0 的杠杆倍数。',
    validationOrderId: '请输入订单 ID。',
  },
  en: {
    eyebrow: 'CCXT Trading',
    title: 'BTC Trading',
    description: 'Review CCXT exchange account state, BTC swap positions and open orders, then submit manual BTC long or short orders.',
    refresh: 'Refresh',
    refreshing: 'Refreshing',
    status: 'API status',
    configured: 'Keys configured',
    notConfigured: 'Keys missing',
    dryRun: 'Dry-run',
    live: 'Live trading',
    sandbox: 'Sandbox',
    demoTrading: 'Demo Trading',
    production: 'Production',
    defaultContract: 'Default contract',
    tradeMode: 'Trade mode',
    safetyTitle: 'Trading guard',
    safetyDryRun: 'Write actions return previews only and do not call exchange write APIs.',
    safetyLive: 'Live trading is enabled. Confirm direction, amount, price, and position side before submitting.',
    configWarningTitle: 'Exchange private API is not configured',
    configWarning: 'Balance, positions, and open orders require the selected exchange API keys: OKX uses OKX_API_KEY / OKX_SECRET / OKX_PASSWORD, and Bybit Demo uses BYBIT_API_KEY / BYBIT_SECRET / BYBIT_DEMO_TRADING=true. Dry-run order previews remain available.',
    balance: 'USDT balance',
    free: 'Free',
    used: 'Used',
    total: 'Total',
    positions: 'BTC positions',
    openOrders: 'Open orders',
    noPositions: 'No BTC positions',
    noOrders: 'No open orders',
    orderTicket: 'Order ticket',
    symbol: 'Contract',
    orderType: 'Order type',
    market: 'Market',
    limit: 'Limit',
    direction: 'Side',
    buyLong: 'Buy / long',
    sellShort: 'Sell / short',
    amount: 'Amount',
    price: 'Price',
    posSide: 'Position side',
    reduceOnly: 'Reduce only',
    clientOrderId: 'Client order id',
    submitOrder: 'Submit order',
    submitting: 'Submitting',
    leveragePanel: 'Leverage & margin',
    leverage: 'Leverage',
    marginMode: 'Margin mode',
    setLeverage: 'Set leverage',
    setMarginMode: 'Set margin',
    cancelPanel: 'Cancel order',
    orderId: 'Order ID',
    cancelOrder: 'Cancel order',
    actionResult: 'Action result',
    loadFailed: 'Failed to load trading data',
    orderSuccess: 'Order submitted',
    orderPreview: 'Order preview',
    cancelSuccess: 'Cancel submitted',
    cancelPreview: 'Cancel preview',
    leverageSuccess: 'Leverage submitted',
    leveragePreview: 'Leverage preview',
    marginSuccess: 'Margin submitted',
    marginPreview: 'Margin preview',
    validationAmount: 'Enter an amount greater than 0.',
    validationPrice: 'Limit orders require a price greater than 0.',
    validationLeverage: 'Enter leverage greater than 0.',
    validationOrderId: 'Enter an order ID.',
  },
} as const;

const fieldClass = 'w-full rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition focus:border-primary/60 focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-60';
const labelClass = 'text-xs font-medium uppercase tracking-[0.08em] text-muted-text';
const panelClass = 'rounded-2xl border border-border/70 bg-card/78 p-4 shadow-soft-card';

function stringifyError(error: unknown): string {
  if (typeof error === 'object' && error && 'parsedApiError' in error) {
    const parsed = (error as { parsedApiError?: { message?: string } }).parsedApiError;
    if (parsed?.message) return parsed.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

function formatNumber(value: unknown, digits = 4): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '--';
  return numeric.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function getTextValue(item: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = item[key];
    if (value !== null && value !== undefined && value !== '') return String(value);
  }
  return '--';
}

function JsonPreview({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto rounded-xl border border-border/60 bg-background/80 p-3 text-xs leading-5 text-secondary-text">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function StatusPill({ active, label }: { active: boolean; label: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium',
        active
          ? 'border-success/30 bg-success/10 text-success'
          : 'border-warning/30 bg-warning/10 text-warning'
      )}
    >
      {label}
    </span>
  );
}

function DataTable({
  title,
  items,
  emptyText,
  kind,
}: {
  title: string;
  items: Record<string, unknown>[];
  emptyText: string;
  kind: 'positions' | 'orders';
}) {
  return (
    <section className={panelClass}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-foreground">{title}</h2>
        <span className="rounded-full bg-muted/70 px-2.5 py-1 text-xs text-secondary-text">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted-text">{emptyText}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] text-left text-sm">
            <thead className="text-xs uppercase tracking-[0.08em] text-muted-text">
              <tr className="border-b border-border/70">
                <th className="py-2 pr-3">Symbol</th>
                <th className="py-2 pr-3">Side</th>
                <th className="py-2 pr-3 text-right">Size</th>
                <th className="py-2 pr-3 text-right">Entry</th>
                <th className="py-2 pr-3 text-right">Mark</th>
                <th className="py-2 text-right">{kind === 'orders' ? 'Status' : 'PnL'}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${getTextValue(item, ['id', 'orderId', 'symbol'])}-${index}`} className="border-b border-border/40 last:border-0">
                  <td className="py-3 pr-3 font-medium text-foreground">{getTextValue(item, ['symbol', 'instId'])}</td>
                  <td className="py-3 pr-3 text-secondary-text">{getTextValue(item, ['side', 'posSide', 'positionSide'])}</td>
                  <td className="py-3 pr-3 text-right text-secondary-text">{getTextValue(item, ['contracts', 'contractSize', 'amount', 'sz'])}</td>
                  <td className="py-3 pr-3 text-right text-secondary-text">{getTextValue(item, ['entryPrice', 'average', 'avgPx', 'price'])}</td>
                  <td className="py-3 pr-3 text-right text-secondary-text">{getTextValue(item, ['markPrice', 'last', 'lastPrice'])}</td>
                  <td className="py-3 text-right text-secondary-text">{getTextValue(item, kind === 'orders' ? ['status', 'state'] : ['unrealizedPnl', 'upl', 'percentage'])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const PortfolioPage: React.FC = () => {
  const { language } = useUiLanguage();
  const copy = text[language];
  const [status, setStatus] = useState<CryptoTradingStatusResponse | null>(null);
  const [balance, setBalance] = useState<CryptoBalanceResponse | null>(null);
  const [positions, setPositions] = useState<CryptoListResponse | null>(null);
  const [openOrders, setOpenOrders] = useState<CryptoListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [lastResult, setLastResult] = useState<unknown>(null);
  const [cancelOrderId, setCancelOrderId] = useState('');
  const [leverage, setLeverage] = useState('3');
  const [marginMode, setMarginMode] = useState<CryptoMarginMode>('cross');
  const [orderForm, setOrderForm] = useState<OrderForm>({
    symbol: DEFAULT_SYMBOL,
    orderType: 'limit',
    side: 'buy',
    amount: '0.01',
    price: '',
    tdMode: 'cross',
    posSide: 'long',
    reduceOnly: false,
    clientOrderId: '',
  });

  const positionItems = positions?.items ?? [];
  const orderItems = openOrders?.items ?? [];
  const defaultSymbol = status?.defaultSymbol || DEFAULT_SYMBOL;

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextStatus = await cryptoTradingApi.getStatus();
      setStatus(nextStatus);
      setOrderForm((current) => ({
        ...current,
        symbol: current.symbol || nextStatus.defaultSymbol || DEFAULT_SYMBOL,
        tdMode: (nextStatus.tdMode as CryptoTradeMode) || current.tdMode,
      }));
      if (nextStatus.tdMode === 'cross' || nextStatus.tdMode === 'isolated') {
        setMarginMode(nextStatus.tdMode);
      }

      if (nextStatus.configured) {
        const [nextBalance, nextPositions, nextOpenOrders] = await Promise.all([
          cryptoTradingApi.getBalance('USDT'),
          cryptoTradingApi.getPositions('BTC'),
          cryptoTradingApi.getOpenOrders('BTC'),
        ]);
        setBalance(nextBalance);
        setPositions(nextPositions);
        setOpenOrders(nextOpenOrders);
      } else {
        setBalance(null);
        setPositions({ exchange: nextStatus.exchange, items: [] });
        setOpenOrders({ exchange: nextStatus.exchange, items: [] });
      }
    } catch (nextError) {
      setError(stringifyError(nextError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const balanceCards = useMemo(
    () => [
      { label: copy.free, value: formatNumber(balance?.free) },
      { label: copy.used, value: formatNumber(balance?.used) },
      { label: copy.total, value: formatNumber(balance?.total) },
    ],
    [balance, copy.free, copy.total, copy.used]
  );

  const runAction = async (key: string, action: () => Promise<TradingActionResult>, titles: { live: string; preview: string }) => {
    setActionLoading(key);
    setFeedback(null);
    try {
      const result = await action();
      setLastResult(result);
      setFeedback({
        tone: result.dryRun ? 'warning' : 'success',
        title: result.dryRun ? titles.preview : titles.live,
        message: result.dryRun ? copy.safetyDryRun : copy.safetyLive,
      });
      if (!result.dryRun) {
        await refresh();
      }
    } catch (nextError) {
      setFeedback({ tone: 'danger', title: copy.actionResult, message: stringifyError(nextError) });
    } finally {
      setActionLoading(null);
    }
  };

  const submitOrder = () => {
    const amount = Number(orderForm.amount);
    const price = Number(orderForm.price);
    if (!Number.isFinite(amount) || amount <= 0) {
      setFeedback({ tone: 'danger', title: copy.actionResult, message: copy.validationAmount });
      return;
    }
    if (orderForm.orderType === 'limit' && (!Number.isFinite(price) || price <= 0)) {
      setFeedback({ tone: 'danger', title: copy.actionResult, message: copy.validationPrice });
      return;
    }
    void runAction(
      'order',
      () =>
        cryptoTradingApi.createOrder({
          symbol: orderForm.symbol || defaultSymbol,
          orderType: orderForm.orderType,
          side: orderForm.side,
          amount,
          price: orderForm.orderType === 'limit' ? price : undefined,
          tdMode: orderForm.tdMode,
          posSide: orderForm.posSide,
          reduceOnly: orderForm.reduceOnly,
          clientOrderId: orderForm.clientOrderId || undefined,
        }),
      { live: copy.orderSuccess, preview: copy.orderPreview }
    );
  };

  const submitCancel = () => {
    if (!cancelOrderId.trim()) {
      setFeedback({ tone: 'danger', title: copy.actionResult, message: copy.validationOrderId });
      return;
    }
    void runAction(
      'cancel',
      () => cryptoTradingApi.cancelOrder({ orderId: cancelOrderId.trim(), symbol: orderForm.symbol || defaultSymbol }),
      { live: copy.cancelSuccess, preview: copy.cancelPreview }
    );
  };

  const submitLeverage = () => {
    const numeric = Number(leverage);
    if (!Number.isFinite(numeric) || numeric <= 0) {
      setFeedback({ tone: 'danger', title: copy.actionResult, message: copy.validationLeverage });
      return;
    }
    void runAction(
      'leverage',
      () =>
        cryptoTradingApi.setLeverage({
          leverage: numeric,
          symbol: orderForm.symbol || defaultSymbol,
          marginMode,
          posSide: orderForm.posSide,
        }),
      { live: copy.leverageSuccess, preview: copy.leveragePreview }
    );
  };

  const submitMarginMode = () => {
    void runAction(
      'margin',
      () => cryptoTradingApi.setMarginMode({ marginMode, symbol: orderForm.symbol || defaultSymbol }),
      { live: copy.marginSuccess, preview: copy.marginPreview }
    );
  };

  return (
    <AppPage className="portfolio-page space-y-5">
      <header className="rounded-2xl border border-border/70 bg-card/80 p-5 shadow-soft-card">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <span className="label-uppercase">{copy.eyebrow}</span>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-foreground md:text-3xl">{copy.title}</h1>
            <p className="mt-2 max-w-3xl text-sm text-secondary-text md:text-base">{copy.description}</p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex h-10 items-center justify-center gap-2 rounded-xl border border-border/70 bg-background/70 px-4 text-sm font-medium text-foreground transition hover:bg-hover disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
            {loading ? copy.refreshing : copy.refresh}
          </button>
        </div>
      </header>

      {error ? <InlineAlert title={copy.loadFailed} message={error} variant="danger" /> : null}
      {status && !status.configured ? <InlineAlert title={copy.configWarningTitle} message={copy.configWarning} variant="warning" /> : null}

      <section className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <div className={panelClass}>
          <div className="mb-4 flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold text-foreground">{copy.status}</h2>
          </div>
          <div className="flex flex-wrap gap-2">
            <StatusPill active={Boolean(status?.configured)} label={status?.configured ? copy.configured : copy.notConfigured} />
            <StatusPill active={Boolean(status?.dryRun)} label={status?.dryRun ? copy.dryRun : copy.live} />
            <StatusPill active={Boolean(status?.sandbox)} label={status?.sandbox ? copy.sandbox : copy.production} />
            {status?.demoTrading ? <StatusPill active={true} label={copy.demoTrading} /> : null}
          </div>
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <div className="rounded-xl border border-border/60 bg-background/50 p-3">
              <p className={labelClass}>{copy.defaultContract}</p>
              <p className="mt-1 text-sm font-medium text-foreground">{status?.exchange ? `${status.exchange.toUpperCase()} · ` : ''}{status?.defaultSymbol ?? DEFAULT_SYMBOL}</p>
            </div>
            <div className="rounded-xl border border-border/60 bg-background/50 p-3">
              <p className={labelClass}>{copy.tradeMode}</p>
              <p className="mt-1 text-sm font-medium text-foreground">{status?.defaultType ?? 'swap'} / {status?.tdMode ?? 'cross'}{status?.settle ? ` / ${status.settle}` : ''}</p>
            </div>
          </div>
          <InlineAlert
            className="mt-4"
            title={copy.safetyTitle}
            message={status?.dryRun === false ? copy.safetyLive : copy.safetyDryRun}
            variant={status?.dryRun === false ? 'danger' : 'info'}
          />
        </div>

        <div className={panelClass}>
          <div className="mb-4 flex items-center gap-2">
            <WalletCards className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold text-foreground">{copy.balance}</h2>
          </div>
          <div className="grid grid-cols-3 gap-3">
            {balanceCards.map((item) => (
              <div key={item.label} className="rounded-xl border border-border/60 bg-background/50 p-3">
                <p className={labelClass}>{item.label}</p>
                <p className="mt-2 text-lg font-semibold text-foreground">{item.value}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <DataTable title={copy.positions} items={positionItems} emptyText={copy.noPositions} kind="positions" />
        <DataTable title={copy.openOrders} items={orderItems} emptyText={copy.noOrders} kind="orders" />
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
        <div className={panelClass}>
          <div className="mb-4 flex items-center gap-2">
            {orderForm.side === 'buy' ? <ArrowDownToLine className="h-5 w-5 text-success" /> : <ArrowUpFromLine className="h-5 w-5 text-danger" />}
            <h2 className="text-base font-semibold text-foreground">{copy.orderTicket}</h2>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.symbol}</span>
              <input className={fieldClass} value={orderForm.symbol} onChange={(event) => setOrderForm((current) => ({ ...current, symbol: event.target.value }))} />
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.orderType}</span>
              <select className={fieldClass} value={orderForm.orderType} onChange={(event) => setOrderForm((current) => ({ ...current, orderType: event.target.value as CryptoOrderType }))}>
                <option value="limit">{copy.limit}</option>
                <option value="market">{copy.market}</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.direction}</span>
              <select
                className={fieldClass}
                value={orderForm.side}
                onChange={(event) => {
                  const side = event.target.value as CryptoOrderSide;
                  setOrderForm((current) => ({ ...current, side, posSide: side === 'buy' ? 'long' : 'short' }));
                }}
              >
                <option value="buy">{copy.buyLong}</option>
                <option value="sell">{copy.sellShort}</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.posSide}</span>
              <select className={fieldClass} value={orderForm.posSide} onChange={(event) => setOrderForm((current) => ({ ...current, posSide: event.target.value as CryptoPositionSide }))}>
                <option value="long">long</option>
                <option value="short">short</option>
                <option value="net">net</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.amount}</span>
              <input className={fieldClass} value={orderForm.amount} type="number" min="0" step="0.001" onChange={(event) => setOrderForm((current) => ({ ...current, amount: event.target.value }))} />
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.price}</span>
              <input className={fieldClass} value={orderForm.price} type="number" min="0" step="0.1" disabled={orderForm.orderType === 'market'} onChange={(event) => setOrderForm((current) => ({ ...current, price: event.target.value }))} />
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.tradeMode}</span>
              <select className={fieldClass} value={orderForm.tdMode} onChange={(event) => setOrderForm((current) => ({ ...current, tdMode: event.target.value as CryptoTradeMode }))}>
                <option value="cross">cross</option>
                <option value="isolated">isolated</option>
                <option value="cash">cash</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.clientOrderId}</span>
              <input className={fieldClass} value={orderForm.clientOrderId} onChange={(event) => setOrderForm((current) => ({ ...current, clientOrderId: event.target.value }))} />
            </label>
            <label className="flex items-center gap-2 text-sm text-secondary-text">
              <input type="checkbox" checked={orderForm.reduceOnly} onChange={(event) => setOrderForm((current) => ({ ...current, reduceOnly: event.target.checked }))} />
              {copy.reduceOnly}
            </label>
          </div>
          <button
            type="button"
            onClick={submitOrder}
            disabled={actionLoading === 'order'}
            className="mt-5 inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-primary px-4 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <SlidersHorizontal className="h-4 w-4" />
            {actionLoading === 'order' ? copy.submitting : copy.submitOrder}
          </button>
        </div>

        <div className="space-y-4">
          <section className={panelClass}>
            <h2 className="mb-4 text-base font-semibold text-foreground">{copy.leveragePanel}</h2>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1.5">
                <span className={labelClass}>{copy.leverage}</span>
                <input className={fieldClass} value={leverage} type="number" min="1" step="1" onChange={(event) => setLeverage(event.target.value)} />
              </label>
              <label className="space-y-1.5">
                <span className={labelClass}>{copy.marginMode}</span>
                <select className={fieldClass} value={marginMode} onChange={(event) => setMarginMode(event.target.value as CryptoMarginMode)}>
                  <option value="cross">cross</option>
                  <option value="isolated">isolated</option>
                </select>
              </label>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="button" onClick={submitLeverage} disabled={actionLoading === 'leverage'} className="inline-flex h-10 items-center justify-center rounded-xl border border-border/70 bg-background/70 px-4 text-sm font-medium text-foreground transition hover:bg-hover disabled:opacity-60">
                {copy.setLeverage}
              </button>
              <button type="button" onClick={submitMarginMode} disabled={actionLoading === 'margin'} className="inline-flex h-10 items-center justify-center rounded-xl border border-border/70 bg-background/70 px-4 text-sm font-medium text-foreground transition hover:bg-hover disabled:opacity-60">
                {copy.setMarginMode}
              </button>
            </div>
          </section>

          <section className={panelClass}>
            <div className="mb-4 flex items-center gap-2">
              <XCircle className="h-5 w-5 text-warning" />
              <h2 className="text-base font-semibold text-foreground">{copy.cancelPanel}</h2>
            </div>
            <label className="space-y-1.5">
              <span className={labelClass}>{copy.orderId}</span>
              <input className={fieldClass} value={cancelOrderId} onChange={(event) => setCancelOrderId(event.target.value)} />
            </label>
            <button type="button" onClick={submitCancel} disabled={actionLoading === 'cancel'} className="mt-4 inline-flex h-10 items-center justify-center rounded-xl border border-warning/30 bg-warning/10 px-4 text-sm font-medium text-warning transition hover:bg-warning/15 disabled:opacity-60">
              {copy.cancelOrder}
            </button>
          </section>
        </div>
      </section>

      {feedback ? <InlineAlert title={feedback.title} message={feedback.message} variant={feedback.tone} /> : null}
      {lastResult ? (
        <section className={panelClass}>
          <h2 className="mb-3 text-base font-semibold text-foreground">{copy.actionResult}</h2>
          <JsonPreview value={lastResult} />
        </section>
      ) : null}
    </AppPage>
  );
};

export default PortfolioPage;
