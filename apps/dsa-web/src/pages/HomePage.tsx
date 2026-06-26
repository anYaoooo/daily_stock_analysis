import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BarChart3, Check, PanelLeftOpen, SlidersHorizontal } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { historyApi } from '../api/history';
import { stocksApi, type KLineData, type StockHistoryPeriod, type StockHistoryResponse, type StockQuoteResponse } from '../api/stocks';
import { agentApi, type SkillInfo } from '../api/agent';
import { systemConfigApi } from '../api/systemConfig';
import { ApiErrorAlert, Button, Drawer, EmptyState, InlineAlert } from '../components/common';
import { DashboardStateBlock } from '../components/dashboard';
import { StockAutocomplete } from '../components/StockAutocomplete';
import { StockHistoryTrendDrawer, StockBar } from '../components/history';
import { ReportMarkdownDrawer } from '../components/report/ReportMarkdownDrawer';
import { ReportSummary } from '../components/report/ReportSummary';
import { RunFlowPanel } from '../components/run-flow';
import { TaskPanel } from '../components/tasks';
import { useDashboardLifecycle, useHomeDashboardState } from '../hooks';
import { useWatchlist } from '../hooks/useWatchlist';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { SetupStatusResponse } from '../types/systemConfig';
import { normalizeReportLanguage } from '../utils/reportLanguage';
import type { TaskInfo } from '../types/analysis';
import type { RunFlowSnapshotSource } from '../types/runFlow';

type RunFlowDrawerState =
  | { open: false }
  | { open: true; source: RunFlowSnapshotSource; title: string };

type StockAnalysisNavigationState = {
  stockCode?: string;
  stockName?: string;
  autoAnalyze?: boolean;
  selectionSource?: string;
};

type CryptoMarketState = {
  isLoading: boolean;
  isHistoryLoading: boolean;
  error: ParsedApiError | null;
  quote: StockQuoteResponse | null;
  history: StockHistoryResponse | null;
  historyPeriod: StockHistoryPeriod;
};

const EMPTY_CRYPTO_MARKET_STATE: CryptoMarketState = {
  isLoading: false,
  isHistoryLoading: false,
  error: null,
  quote: null,
  history: null,
  historyPeriod: 'daily',
};

type BitcoinHistoryPeriodLabelKey =
  | 'home.bitcoinPeriodHourly'
  | 'home.bitcoinPeriodFourHour'
  | 'home.bitcoinPeriodDaily'
  | 'home.bitcoinPeriodWeekly'
  | 'home.bitcoinPeriodMonthly';

const BITCOIN_HISTORY_PERIOD_OPTIONS: Array<{
  value: StockHistoryPeriod;
  labelKey: BitcoinHistoryPeriodLabelKey;
  days: number;
}> = [
  { value: 'hourly', labelKey: 'home.bitcoinPeriodHourly', days: 7 },
  { value: 'four_hour', labelKey: 'home.bitcoinPeriodFourHour', days: 30 },
  { value: 'daily', labelKey: 'home.bitcoinPeriodDaily', days: 30 },
  { value: 'weekly', labelKey: 'home.bitcoinPeriodWeekly', days: 180 },
  { value: 'monthly', labelKey: 'home.bitcoinPeriodMonthly', days: 365 },
];

const getBitcoinHistoryPeriodDays = (period: StockHistoryPeriod) => (
  BITCOIN_HISTORY_PERIOD_OPTIONS.find((option) => option.value === period)?.days ?? 30
);

const getBitcoinHistoryPeriodLabelKey = (period: StockHistoryPeriod): BitcoinHistoryPeriodLabelKey => (
  BITCOIN_HISTORY_PERIOD_OPTIONS.find((option) => option.value === period)?.labelKey ?? 'home.bitcoinPeriodDaily'
);

const formatMarketNumber = (
  value: number | null | undefined,
  language: string,
  options: Intl.NumberFormatOptions = {},
) => {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return '--';
  }
  return new Intl.NumberFormat(language === 'en' ? 'en-US' : 'zh-CN', options).format(value);
};

const formatMarketPercent = (value: number | null | undefined, language: string) => {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return '--';
  }
  const sign = value > 0 ? '+' : '';
  return `${sign}${formatMarketNumber(value, language, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
};

const formatMarketPrice = (value: number | null | undefined, language: string) => (
  formatMarketNumber(value, language, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
);

const BitcoinCandlestickChart: React.FC<{
  items: KLineData[];
  language: string;
  emptyText: string;
}> = ({ items, language, emptyText }) => {
  if (items.length === 0) {
    return (
      <div className="flex h-56 items-center justify-center rounded-lg border border-dashed border-subtle text-sm text-muted-text">
        {emptyText}
      </div>
    );
  }

  const width = 760;
  const height = 260;
  const padding = { top: 18, right: 64, bottom: 34, left: 16 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const highs = items.map((item) => item.high);
  const lows = items.map((item) => item.low);
  const rawMax = Math.max(...highs);
  const rawMin = Math.min(...lows);
  const rangePadding = Math.max((rawMax - rawMin) * 0.08, 1);
  const max = rawMax + rangePadding;
  const min = rawMin - rangePadding;
  const range = Math.max(max - min, 1);
  const step = plotWidth / Math.max(items.length, 1);
  const candleWidth = Math.min(Math.max(step * 0.48, 6), 18);
  const yFor = (value: number) => padding.top + ((max - value) / range) * plotHeight;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => max - range * ratio);
  const labelIndexes = Array.from(new Set([0, Math.floor((items.length - 1) / 2), items.length - 1]));

  return (
    <div className="overflow-x-auto rounded-lg border border-subtle bg-surface/40">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Bitcoin candlestick chart"
        className="block min-w-[42rem] w-full"
      >
        <g className="text-muted-text">
          {ticks.map((tick) => {
            const y = yFor(tick);
            return (
              <g key={tick}>
                <line
                  x1={padding.left}
                  x2={width - padding.right}
                  y1={y}
                  y2={y}
                  stroke="currentColor"
                  strokeOpacity="0.16"
                />
                <text
                  x={width - padding.right + 8}
                  y={y + 4}
                  fill="currentColor"
                  fontSize="11"
                >
                  {formatMarketNumber(tick, language, { maximumFractionDigits: 0 })}
                </text>
              </g>
            );
          })}
        </g>
        {items.map((item, index) => {
          const x = padding.left + step * index + step / 2;
          const openY = yFor(item.open);
          const closeY = yFor(item.close);
          const highY = yFor(item.high);
          const lowY = yFor(item.low);
          const bodyY = Math.min(openY, closeY);
          const bodyHeight = Math.max(Math.abs(closeY - openY), 2);
          const isUp = item.close >= item.open;
          return (
            <g key={item.date} className={isUp ? 'text-success' : 'text-danger'}>
              <line
                x1={x}
                x2={x}
                y1={highY}
                y2={lowY}
                stroke="currentColor"
                strokeWidth="1.5"
              />
              <rect
                x={x - candleWidth / 2}
                y={bodyY}
                width={candleWidth}
                height={bodyHeight}
                rx="1"
                fill="currentColor"
                fillOpacity={isUp ? '0.78' : '0.9'}
              />
            </g>
          );
        })}
        <g className="text-muted-text">
          {labelIndexes.map((index) => {
            const item = items[index];
            const x = padding.left + step * index + step / 2;
            return (
              <text
                key={item.date}
                x={x}
                y={height - 12}
                fill="currentColor"
                fontSize="11"
                textAnchor="middle"
              >
                {item.date.slice(5)}
              </text>
            );
          })}
        </g>
      </svg>
    </div>
  );
};

const HomePage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { language: uiLanguage, t } = useUiLanguage();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [stockBarVisible, setStockBarVisible] = useState(true);
  const [cryptoMarket, setCryptoMarket] = useState<CryptoMarketState>(EMPTY_CRYPTO_MARKET_STATE);
  const [analysisSkills, setAnalysisSkills] = useState<SkillInfo[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState('');
  const [strategyMenuOpen, setStrategyMenuOpen] = useState(false);
  const [runFlowDrawer, setRunFlowDrawer] = useState<RunFlowDrawerState>({ open: false });
  const dashboardScrollRef = useRef<HTMLElement | null>(null);
  const strategyMenuRef = useRef<HTMLDivElement | null>(null);
  const strategyButtonRef = useRef<HTMLButtonElement | null>(null);
  const strategyItemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const strategyInitialFocusIndexRef = useRef<number | null>(null);

  const scrollDashboardTop = useCallback(() => {
    const scrollContainer = dashboardScrollRef.current;
    if (!scrollContainer) {
      return;
    }

    if (typeof scrollContainer.scrollTo === 'function') {
      scrollContainer.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    scrollContainer.scrollTop = 0;
  }, []);

  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);

  const {
    query,
    inputError,
    duplicateError,
    error,
    isAnalyzing,
    selectedReport,
    isLoadingReport,
    isHistoryTrendOpen,
    stockHistoryItems,
    stockHistoryTotal,
    stockHistoryHasMore,
    isLoadingStockHistory,
    isLoadingMoreStockHistory,
    stockHistoryError,
    stockHistoryFilters,
    activeTasks,
    markdownDrawerOpen,
    setQuery,
    clearError,
    loadInitialHistory,
    refreshHistory,
    selectHistoryItem,
    submitAnalysis,
    notify,
    setNotify,
    syncTaskCreated,
    syncTaskUpdated,
    syncTaskFailed,
    refreshActiveTasks,
    removeTask,
    openMarkdownDrawer,
    closeMarkdownDrawer,
    openHistoryTrend,
    closeHistoryTrend,
    setStockHistoryRange,
    loadMoreStockHistory,
    stockBarItems,
    isLoadingStockBar,
    loadStockBar,
    refreshStockBar,
  } = useHomeDashboardState();

  useEffect(() => {
    document.title = t('home.pageTitle');
  }, [t]);

  useEffect(() => {
    let active = true;
    systemConfigApi.getSetupStatus()
      .then((status) => {
        if (active) {
          setSetupStatus(status);
        }
      })
      .catch(() => {
        if (active) {
          setSetupStatus(null);
        }
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    agentApi.getSkills()
      .then((response) => {
        if (active) {
          setAnalysisSkills(response.skills);
        }
      })
      .catch(() => {
        if (active) {
          setAnalysisSkills([]);
        }
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!strategyMenuOpen) {
      return;
    }

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && strategyMenuRef.current?.contains(target)) {
        return;
      }
      setStrategyMenuOpen(false);
    };

    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [strategyMenuOpen]);

  useEffect(() => {
    if (selectedStrategyId && !analysisSkills.some((skill) => skill.id === selectedStrategyId)) {
      setSelectedStrategyId('');
    }
  }, [analysisSkills, selectedStrategyId]);

  const reportLanguage = normalizeReportLanguage(selectedReport?.meta.reportLanguage);
  const isMarketReviewHistoryReport = selectedReport?.meta.reportType === 'market_review';
  const isHistoryTrendUnavailable = !selectedReport || !selectedReport.meta.stockCode;
  const bitcoinKLines = useMemo(
    () => (cryptoMarket.history?.data ?? []).slice(-10).reverse(),
    [cryptoMarket.history],
  );
  const bitcoinChartItems = useMemo(
    () => (cryptoMarket.history?.data ?? []).slice(-30),
    [cryptoMarket.history],
  );

  useEffect(() => {
    if (!isHistoryTrendUnavailable || !isHistoryTrendOpen) {
      return;
    }
    closeHistoryTrend();
  }, [closeHistoryTrend, isHistoryTrendOpen, isHistoryTrendUnavailable]);

  const selectedStrategy = useMemo(
    () => analysisSkills.find((skill) => skill.id === selectedStrategyId),
    [analysisSkills, selectedStrategyId],
  );
  const selectedAnalysisSkills = useMemo(
    () => (selectedStrategyId ? [selectedStrategyId] : undefined),
    [selectedStrategyId],
  );
  const strategyOptions = useMemo(
    () => [
      { id: '', name: t('home.defaultStrategyName'), description: t('home.defaultStrategyDescription') },
      ...analysisSkills.map((skill) => ({
        id: skill.id,
        name: skill.name,
        description: skill.description,
      })),
    ],
    [analysisSkills, t],
  );
  const closeStrategyMenu = useCallback((restoreFocus = false) => {
    setStrategyMenuOpen(false);
    if (restoreFocus) {
      strategyButtonRef.current?.focus();
    }
  }, []);
  const selectStrategy = useCallback((strategyId: string) => {
    setSelectedStrategyId(strategyId);
    setStrategyMenuOpen(false);
  }, []);
  const focusStrategyItem = useCallback((index: number) => {
    const itemCount = strategyOptions.length;
    if (itemCount === 0) {
      return;
    }
    const nextIndex = (index + itemCount) % itemCount;
    strategyItemRefs.current[nextIndex]?.focus();
  }, [strategyOptions.length]);
  const getSelectedStrategyIndex = useCallback(() => {
    const selectedIndex = strategyOptions.findIndex((option) => option.id === selectedStrategyId);
    return selectedIndex >= 0 ? selectedIndex : 0;
  }, [selectedStrategyId, strategyOptions]);
  useEffect(() => {
    strategyItemRefs.current = strategyItemRefs.current.slice(0, strategyOptions.length);
  }, [strategyOptions.length]);
  useEffect(() => {
    if (!strategyMenuOpen) {
      return undefined;
    }

    const targetIndex = strategyInitialFocusIndexRef.current ?? getSelectedStrategyIndex();
    strategyInitialFocusIndexRef.current = null;
    const timeout = window.setTimeout(() => focusStrategyItem(targetIndex), 0);
    return () => window.clearTimeout(timeout);
  }, [focusStrategyItem, getSelectedStrategyIndex, strategyMenuOpen]);
  const handleStrategyButtonKeyDown = useCallback((event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') {
      return;
    }

    event.preventDefault();
    const targetIndex = event.key === 'ArrowUp' ? strategyOptions.length - 1 : 0;
    if (strategyMenuOpen) {
      focusStrategyItem(targetIndex);
      return;
    }
    strategyInitialFocusIndexRef.current = targetIndex;
    setStrategyMenuOpen(true);
  }, [focusStrategyItem, strategyMenuOpen, strategyOptions.length]);
  const handleStrategyMenuKeyDown = useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    const itemCount = strategyOptions.length;
    if (itemCount === 0) {
      return;
    }

    const currentIndex = strategyItemRefs.current.findIndex((item) => item === document.activeElement);
    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        closeStrategyMenu(true);
        break;
      case 'ArrowDown':
        event.preventDefault();
        focusStrategyItem(currentIndex >= 0 ? currentIndex + 1 : 0);
        break;
      case 'ArrowUp':
        event.preventDefault();
        focusStrategyItem(currentIndex >= 0 ? currentIndex - 1 : itemCount - 1);
        break;
      case 'Home':
        event.preventDefault();
        focusStrategyItem(0);
        break;
      case 'End':
        event.preventDefault();
        focusStrategyItem(itemCount - 1);
        break;
      case 'Tab':
        setStrategyMenuOpen(false);
        break;
      default:
        break;
    }
  }, [closeStrategyMenu, focusStrategyItem, strategyOptions.length]);
  const setupNeedsAction = setupStatus ? !setupStatus.isComplete : false;
  const requestedHistoryId = useMemo(() => {
    const params = new URLSearchParams(location.search);
    const raw = params.get('history');
    if (!raw) {
      return null;
    }
    const parsed = Number.parseInt(raw, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }, [location.search]);

  const setupMissingLabels = useMemo(() => {
    if (!setupStatus) {
      return '';
    }
    const requiredNeedsAction = setupStatus.checks
      .filter((check) => check.required && check.status === 'needs_action')
      .map((check) => check.title);
    return requiredNeedsAction.slice(0, 3).join(uiLanguage === 'en' ? ', ' : '、');
  }, [setupStatus, uiLanguage]);

  const loadInitialHistoryForRoute = useCallback(async () => {
    if (requestedHistoryId) {
      await refreshHistory();
      return;
    }
    await loadInitialHistory();
  }, [loadInitialHistory, refreshHistory, requestedHistoryId]);

  useDashboardLifecycle({
    loadInitialHistory: loadInitialHistoryForRoute,
    refreshHistory,
    loadStockBar,
    refreshStockBar,
    syncTaskCreated,
    syncTaskUpdated,
    syncTaskFailed,
    refreshActiveTasks,
    removeTask,
  });

  useEffect(() => {
    if (!requestedHistoryId) {
      return;
    }
    void selectHistoryItem(requestedHistoryId).then(() => {
      scrollDashboardTop();
    });
  }, [requestedHistoryId, scrollDashboardTop, selectHistoryItem]);

  const watchlistState = useWatchlist();

  const handleHistoryItemClick = useCallback((recordId: number) => {
    void selectHistoryItem(recordId);
    setSidebarOpen(false);
  }, [selectHistoryItem]);

  const [isDeletingStock, setIsDeletingStock] = useState(false);
  const handleDeleteStockRecord = useCallback(async (recordId: number) => {
    if (isDeletingStock) return;
    setIsDeletingStock(true);
    try {
      await historyApi.deleteRecords([recordId]);
      await refreshStockBar();
      await refreshHistory(true);
    } catch {
      // error silently ignored
    } finally {
      setIsDeletingStock(false);
    }
  }, [isDeletingStock, refreshStockBar, refreshHistory]);

  const handleSubmitAnalysis = useCallback(
    (
      stockCode?: string,
      stockName?: string,
      selectionSource?: 'manual' | 'autocomplete' | 'import' | 'image',
    ) => {
      void submitAnalysis({
        stockCode,
        stockName,
        originalQuery: query,
        selectionSource: selectionSource ?? 'manual',
        skills: selectedAnalysisSkills,
      });
    },
    [query, selectedAnalysisSkills, submitAnalysis],
  );

  useEffect(() => {
    const state = location.state as StockAnalysisNavigationState | null;
    const stockCode = typeof state?.stockCode === 'string' ? state.stockCode.trim() : '';
    if (!stockCode) {
      return;
    }
    const stockName = typeof state?.stockName === 'string' ? state.stockName.trim() : '';
    setQuery(stockCode);
    navigate(location.pathname, { replace: true, state: null });
    if (state?.autoAnalyze) {
      handleSubmitAnalysis(stockCode, stockName || undefined, 'import');
    }
  }, [handleSubmitAnalysis, location.pathname, location.state, navigate, setQuery]);

  const handleAskFollowUp = useCallback(() => {
    if (selectedReport?.meta.id === undefined || selectedReport.meta.reportType === 'market_review') {
      return;
    }

    const code = selectedReport.meta.stockCode;
    const name = selectedReport.meta.stockName;
    const rid = selectedReport.meta.id;
    navigate(`/chat?stock=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}&recordId=${rid}`);
  }, [navigate, selectedReport]);

  const handleReanalyze = useCallback(() => {
    if (!selectedReport || selectedReport.meta.reportType === 'market_review') {
      return;
    }

    void submitAnalysis({
      stockCode: selectedReport.meta.stockCode,
      stockName: selectedReport.meta.stockName,
      originalQuery: selectedReport.meta.stockCode,
      selectionSource: 'manual',
      forceRefresh: true,
      skills: selectedAnalysisSkills,
    });
  }, [selectedAnalysisSkills, selectedReport, submitAnalysis]);

  const openTaskRunFlow = useCallback((task: TaskInfo) => {
    const stock = task.stockName || task.stockCode || task.taskId;
    setRunFlowDrawer({
      open: true,
      source: { type: 'task', taskId: task.taskId },
      title: t('runFlow.taskDrawerTitle', { stock }),
    });
  }, [t]);

  const openHistoryRunFlow = useCallback((recordId: number) => {
    const meta = selectedReport?.meta.id === recordId ? selectedReport.meta : null;
    const stock = meta?.stockName || meta?.stockCode || String(recordId);
    setRunFlowDrawer({
      open: true,
      source: { type: 'history', recordId },
      title: t('runFlow.historyDrawerTitle', { stock }),
    });
  }, [selectedReport, t]);

  const closeRunFlowDrawer = useCallback(() => {
    setRunFlowDrawer({ open: false });
  }, []);

  const loadBitcoinHistory = useCallback(async (period: StockHistoryPeriod) => {
    setCryptoMarket((current) => ({
      ...current,
      isHistoryLoading: true,
      historyPeriod: period,
      error: null,
    }));
    try {
      const history = await stocksApi.getHistory('BTC', {
        period,
        days: getBitcoinHistoryPeriodDays(period),
      });
      setCryptoMarket((current) => ({
        ...current,
        isHistoryLoading: false,
        error: null,
        history,
        historyPeriod: period,
      }));
    } catch (err: unknown) {
      setCryptoMarket((current) => ({
        ...current,
        isHistoryLoading: false,
        error: getParsedApiError(err),
      }));
    }
  }, []);

  const handleLoadBitcoinMarket = useCallback(async () => {
    const period: StockHistoryPeriod = 'daily';
    setCryptoMarket((current) => ({
      ...current,
      isLoading: true,
      isHistoryLoading: true,
      historyPeriod: period,
      error: null,
    }));
    scrollDashboardTop();
    try {
      const [quote, history] = await Promise.all([
        stocksApi.getQuote('BTC', { includeNews: true }),
        stocksApi.getHistory('BTC', { period, days: getBitcoinHistoryPeriodDays(period) }),
      ]);
      setCryptoMarket({
        isLoading: false,
        isHistoryLoading: false,
        error: null,
        quote,
        history,
        historyPeriod: period,
      });
    } catch (err: unknown) {
      setCryptoMarket((current) => ({
        ...current,
        isLoading: false,
        isHistoryLoading: false,
        error: getParsedApiError(err),
      }));
    }
  }, [scrollDashboardTop]);

  const visibleStockBarItems = useMemo(
    () => stockBarItems.filter((item) => item.stockCode !== 'MARKET' && item.reportType !== 'market_review'),
    [stockBarItems],
  );

  const sidebarContent = useMemo(
    () => (
      <div className="flex min-h-0 h-full flex-col gap-3 overflow-hidden">
        <TaskPanel tasks={activeTasks} onOpenRunFlow={openTaskRunFlow} />
        {stockBarVisible ? (
          <StockBar
            items={visibleStockBarItems}
            isLoading={isLoadingStockBar}
            selectedStockCode={selectedReport?.meta.stockCode}
            selectedRecordId={selectedReport?.meta.id}
            onItemClick={handleHistoryItemClick}
            onDeleteRecord={handleDeleteStockRecord}
            onClose={() => setStockBarVisible(false)}
            isDeleting={isDeletingStock}
            className="flex-1 overflow-hidden"
          />
        ) : (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => setStockBarVisible(true)}
            className="w-full justify-start"
            aria-label={t('stockBar.show')}
          >
            <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
            {t('stockBar.show')}
          </Button>
        )}
      </div>
    ),
    [
      activeTasks,
      isLoadingStockBar,
      handleHistoryItemClick,
      handleDeleteStockRecord,
      isDeletingStock,
      openTaskRunFlow,
      selectedReport?.meta.stockCode,
      selectedReport?.meta.id,
      stockBarVisible,
      t,
      visibleStockBarItems,
    ],
  );

  return (
    <div
      data-testid="home-dashboard"
      className="flex h-[calc(100vh-5rem)] w-full flex-col overflow-hidden md:flex-row sm:h-[calc(100vh-5.5rem)] lg:h-[calc(100vh-2rem)]"
    >
      <div className="flex-1 flex flex-col min-h-0 min-w-0 max-w-full lg:max-w-6xl mx-auto w-full">
        <header className="relative z-30 flex min-w-0 flex-shrink-0 items-center overflow-visible px-3 py-3 md:px-4 md:py-4">
          <div className="flex min-w-0 flex-1 flex-col gap-2.5 md:flex-row md:items-center">
            <div className="flex min-w-0 flex-1 items-center gap-2.5">
              <button
                onClick={() => setSidebarOpen(true)}
                className="md:hidden -ml-1 flex-shrink-0 rounded-lg p-1.5 text-secondary-text transition-colors hover:bg-hover hover:text-foreground"
                aria-label={t('home.historyButton')}
              >
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
              <div className="relative min-w-0 flex-1">
                <StockAutocomplete
                  value={query}
                  onChange={setQuery}
                  onSubmit={(stockCode, stockName, selectionSource) => {
                    handleSubmitAnalysis(stockCode, stockName, selectionSource);
                  }}
                  placeholder={t('home.placeholder')}
                  disabled={isAnalyzing}
                  className={inputError ? 'border-danger/50' : undefined}
                />
              </div>
              {analysisSkills.length > 0 ? (
                <div ref={strategyMenuRef} className="relative flex-shrink-0">
                  <button
                    ref={strategyButtonRef}
                    id="strategy-menu-button"
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={strategyMenuOpen}
                    aria-controls={strategyMenuOpen ? 'strategy-menu' : undefined}
                    onClick={() => setStrategyMenuOpen((open) => !open)}
                    onKeyDown={handleStrategyButtonKeyDown}
                    disabled={isAnalyzing}
                    className="home-surface-button flex h-10 max-w-[8.5rem] items-center gap-1.5 rounded-xl px-3 text-xs text-foreground disabled:cursor-not-allowed disabled:opacity-60 sm:max-w-[11rem]"
                  >
                    <SlidersHorizontal className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
                    <span className="truncate">{selectedStrategy?.name || t('home.strategy')}</span>
                  </button>
                  {strategyMenuOpen ? (
                    <div
                      id="strategy-menu"
                      role="menu"
                      aria-labelledby="strategy-menu-button"
                      onKeyDown={handleStrategyMenuKeyDown}
                      className="absolute right-0 top-11 z-[120] max-h-80 w-[min(18rem,calc(100vw-1.5rem))] overflow-y-auto rounded-xl border border-subtle bg-elevated p-1.5 text-sm text-foreground shadow-2xl"
                    >
                      {strategyOptions.map((option, index) => {
                        const selected = selectedStrategyId === option.id;
                        return (
                          <button
                            key={option.id || 'default'}
                            ref={(node) => {
                              strategyItemRefs.current[index] = node;
                            }}
                            type="button"
                            role="menuitemradio"
                            aria-checked={selected}
                            tabIndex={-1}
                            onClick={() => selectStrategy(option.id)}
                            className="flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-hover"
                          >
                            <Check className={`mt-0.5 h-4 w-4 flex-shrink-0 ${selected ? 'opacity-100' : 'opacity-0'}`} aria-hidden="true" />
                            <span className="min-w-0">
                              <span className="block font-medium">{option.name}</span>
                              <span className="mt-0.5 line-clamp-2 block text-xs leading-5 text-muted-text">{option.description}</span>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
            <div className="flex min-w-0 flex-shrink-0 items-center gap-2.5">
              <label className="flex h-10 flex-shrink-0 cursor-pointer items-center gap-1.5 rounded-xl border border-subtle bg-surface/60 px-3 text-xs text-secondary-text select-none transition-colors hover:border-subtle-hover hover:text-foreground">
                <input
                  type="checkbox"
                  checked={notify}
                  onChange={(e) => setNotify(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-border accent-primary"
                />
                {t('home.notify')}
              </label>
              <Button
                type="button"
                variant="secondary"
                size="md"
                isLoading={cryptoMarket.isLoading}
                loadingText={t('home.bitcoinLoading')}
                onClick={() => void handleLoadBitcoinMarket()}
                className="h-10 flex-1 whitespace-nowrap md:flex-none"
              >
                <BarChart3 className="h-4 w-4" aria-hidden="true" />
                {t('home.bitcoinMarket')}
              </Button>
              <button
                type="button"
                onClick={() => handleSubmitAnalysis()}
                disabled={!query || isAnalyzing}
                className="btn-primary flex h-10 flex-1 items-center justify-center gap-1.5 whitespace-nowrap md:flex-none"
              >
                {isAnalyzing ? (
                  <>
                    <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                    </svg>
                    {t('home.analyzing')}
                  </>
                ) : (
                  t('home.analyze')
                )}
              </button>
            </div>
          </div>
        </header>

        {inputError || duplicateError ? (
          <div className="px-3 pb-2 md:px-4">
            {inputError ? (
              <InlineAlert
                variant="danger"
                title={t('home.inputInvalid')}
                message={inputError}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}
            {!inputError && duplicateError ? (
              <InlineAlert
                variant="warning"
                title={t('home.duplicateTask')}
                message={duplicateError}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}
          </div>
        ) : null}

        {setupNeedsAction ? (
          <div className="px-3 pb-2 md:px-4">
            <InlineAlert
              variant="warning"
              title={t('home.setupIncomplete')}
              message={
                setupMissingLabels
                  ? t('home.setupMissingWithLabels', { labels: setupMissingLabels })
                  : t('home.setupMissingGeneric')
              }
              action={(
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => navigate('/settings')}
                >
                  {t('home.goSettings')}
                </Button>
              )}
              className="rounded-xl px-3 py-2 text-xs shadow-none"
            />
          </div>
        ) : null}

        <div className="flex-1 flex min-h-0 overflow-hidden">
          <div className="hidden min-h-0 w-64 shrink-0 flex-col overflow-hidden pl-4 pb-4 md:flex lg:w-72">
            {sidebarContent}
          </div>

          {sidebarOpen ? (
            <div className="fixed inset-0 z-40 md:hidden" onClick={() => setSidebarOpen(false)}>
              <div className="page-drawer-overlay absolute inset-0" />
              <div
                className="dashboard-card absolute bottom-0 left-0 top-0 flex w-72 flex-col overflow-hidden !rounded-none !rounded-r-xl p-3 shadow-2xl"
                onClick={(event) => event.stopPropagation()}
              >
                {sidebarContent}
              </div>
            </div>
          ) : null}

          <section
            ref={dashboardScrollRef}
            data-testid="home-dashboard-scroll"
            className="flex-1 min-w-0 min-h-0 overflow-x-auto overflow-y-auto px-3 pb-4 md:px-6 touch-pan-y"
          >
            {cryptoMarket.error ? (
              <div className="mb-3">
                <ApiErrorAlert
                  error={cryptoMarket.error}
                  className="mb-1"
                  onDismiss={() => setCryptoMarket((current) => ({ ...current, error: null }))}
                />
              </div>
            ) : null}

            {cryptoMarket.quote ? (
              <div
                className="dashboard-card mb-3 max-w-6xl p-4"
                data-testid="bitcoin-market-panel"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="label-uppercase">{t('home.bitcoinSource')}</p>
                    <h2 className="mt-1 text-lg font-semibold text-foreground">
                      {cryptoMarket.quote.stockName || 'Bitcoin'} ({cryptoMarket.quote.stockCode || 'BTCUSDT'})
                    </h2>
                  </div>
                  <div className="flex flex-wrap items-start justify-end gap-3">
                    <div className="text-right">
                      <p className="text-2xl font-semibold text-foreground">
                        {formatMarketPrice(cryptoMarket.quote.currentPrice, uiLanguage)}
                      </p>
                      <p
                        className={`mt-1 text-sm font-medium ${
                          (cryptoMarket.quote.changePercent ?? 0) >= 0 ? 'text-success' : 'text-danger'
                        }`}
                      >
                        {formatMarketNumber(cryptoMarket.quote.change, uiLanguage, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        {' / '}
                        {formatMarketPercent(cryptoMarket.quote.changePercent, uiLanguage)}
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="home-action-ai"
                      size="sm"
                      disabled={isAnalyzing}
                      onClick={() => handleSubmitAnalysis('BTC', 'Bitcoin', 'manual')}
                    >
                      <BarChart3 className="h-4 w-4" aria-hidden="true" />
                      {t('home.bitcoinAnalyze')}
                    </Button>
                  </div>
                </div>
                <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  {[
                    { key: 'open', label: t('home.bitcoinOpen'), value: cryptoMarket.quote.open },
                    { key: 'high', label: t('home.bitcoinHigh'), value: cryptoMarket.quote.high },
                    { key: 'low', label: t('home.bitcoinLow'), value: cryptoMarket.quote.low },
                    { key: 'amount', label: t('home.bitcoinAmount'), value: cryptoMarket.quote.amount },
                  ].map((item) => (
                    <div key={item.key} className="rounded-lg border border-subtle bg-surface/50 px-3 py-2">
                      <p className="label-uppercase">{item.label}</p>
                      <p className="mt-1 text-sm font-semibold text-foreground">
                        {formatMarketNumber(item.value, uiLanguage, {
                          maximumFractionDigits: item.key === 'amount' ? 0 : 2,
                          minimumFractionDigits: item.key === 'amount' ? 0 : 2,
                        })}
                      </p>
                    </div>
                  ))}
                </div>
                {cryptoMarket.quote.news && cryptoMarket.quote.news.length > 0 ? (
                  <div className="mt-4 rounded-lg border border-subtle bg-surface/40 p-3">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <p className="label-uppercase">{t('home.bitcoinNews')}</p>
                      <p className="text-xs text-muted-text">{t('home.bitcoinNewsSource')}</p>
                    </div>
                    <div className="grid gap-2">
                      {cryptoMarket.quote.news.slice(0, 3).map((item, index) => (
                        <article
                          key={`${item.url || item.title}-${index}`}
                          className="rounded-md border border-subtle bg-background/40 px-3 py-2"
                        >
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-text">
                            {item.source ? <span>{item.source}</span> : null}
                            {item.publishedDate ? <span>{item.publishedDate}</span> : null}
                          </div>
                          {item.url ? (
                            <a
                              href={item.url}
                              target="_blank"
                              rel="noreferrer"
                              className="mt-1 block text-sm font-semibold text-foreground hover:text-primary"
                            >
                              {item.translatedTitle || item.title}
                            </a>
                          ) : (
                            <p className="mt-1 text-sm font-semibold text-foreground">{item.translatedTitle || item.title}</p>
                          )}
                          {item.summaryZh || item.snippet ? (
                            <p className="mt-1 line-clamp-2 text-xs leading-5 text-secondary-text">
                              {item.summaryZh || item.snippet}
                            </p>
                          ) : null}
                          {item.translatedTitle && item.translatedTitle !== item.title ? (
                            <p className="mt-1 text-[11px] leading-4 text-muted-text">{item.title}</p>
                          ) : null}
                        </article>
                      ))}
                    </div>
                  </div>
                ) : null}
                <div className="mt-4">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="label-uppercase">{t('home.bitcoinKLineChart')}</p>
                      <p className="mt-1 text-xs text-muted-text">
                        {t('home.bitcoinKLineWindow', {
                          period: t(getBitcoinHistoryPeriodLabelKey(cryptoMarket.historyPeriod)),
                        })}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-1 rounded-lg border border-subtle bg-surface/50 p-1">
                      {BITCOIN_HISTORY_PERIOD_OPTIONS.map((option) => {
                        const selected = cryptoMarket.historyPeriod === option.value;
                        return (
                          <button
                            key={option.value}
                            type="button"
                            disabled={cryptoMarket.isLoading || cryptoMarket.isHistoryLoading}
                            onClick={() => void loadBitcoinHistory(option.value)}
                            className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                              selected
                                ? 'bg-primary text-primary-foreground'
                                : 'text-secondary-text hover:bg-hover hover:text-foreground'
                            }`}
                          >
                            {t(option.labelKey)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  {cryptoMarket.isHistoryLoading ? (
                    <div className="flex h-56 items-center justify-center rounded-lg border border-dashed border-subtle text-sm text-muted-text">
                      {t('home.bitcoinKLineLoading')}
                    </div>
                  ) : (
                    <BitcoinCandlestickChart
                      items={bitcoinChartItems}
                      language={uiLanguage}
                      emptyText={t('home.bitcoinKLineEmpty')}
                    />
                  )}
                </div>
                <div className="mt-4 overflow-x-auto rounded-lg border border-subtle">
                  <table className="min-w-[40rem] w-full text-left text-xs">
                    <thead className="bg-surface/70 text-muted-text">
                      <tr>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinDate')}</th>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinOpen')}</th>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinHigh')}</th>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinLow')}</th>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinClose')}</th>
                        <th className="px-3 py-2 font-medium">{t('home.bitcoinChangePercent')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-subtle">
                      {bitcoinKLines.map((item) => (
                        <tr key={item.date} className="text-secondary-text">
                          <td className="px-3 py-2 font-medium text-foreground">{item.date}</td>
                          <td className="px-3 py-2">{formatMarketPrice(item.open, uiLanguage)}</td>
                          <td className="px-3 py-2">{formatMarketPrice(item.high, uiLanguage)}</td>
                          <td className="px-3 py-2">{formatMarketPrice(item.low, uiLanguage)}</td>
                          <td className="px-3 py-2">{formatMarketPrice(item.close, uiLanguage)}</td>
                          <td className={`px-3 py-2 font-medium ${(item.changePercent ?? 0) >= 0 ? 'text-success' : 'text-danger'}`}>
                            {formatMarketPercent(item.changePercent, uiLanguage)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {cryptoMarket.quote.updateTime ? (
                  <p className="mt-3 text-xs text-muted-text">
                    {t('home.bitcoinUpdatedAt', { time: cryptoMarket.quote.updateTime })}
                  </p>
                ) : null}
              </div>
            ) : null}

            {error ? (
              <ApiErrorAlert
                error={error}
                className="mb-3"
                onDismiss={clearError}
              />
            ) : null}
            {isLoadingReport ? (
              <div className="flex h-full flex-col items-center justify-center">
                <DashboardStateBlock title={t('home.loadingReport')} loading />
              </div>
            ) : selectedReport ? (
              <div className={isHistoryTrendOpen ? 'max-w-6xl space-y-4 pb-8' : 'max-w-4xl space-y-4 pb-8'}>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {!isMarketReviewHistoryReport ? (
                    <>
                      <Button
                        variant="home-action-ai"
                        size="sm"
                        disabled={isAnalyzing || selectedReport.meta.id === undefined}
                        onClick={handleReanalyze}
                      >
                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                        </svg>
                        {t('home.reanalyze')}
                      </Button>
                      <Button
                        variant="home-action-ai"
                        size="sm"
                        disabled={selectedReport.meta.id === undefined}
                        onClick={handleAskFollowUp}
                      >
                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                        </svg>
                        {t('home.askAi')}
                      </Button>
                    </>
                  ) : null}
                  <Button
                    variant="home-action-ai"
                    size="sm"
                    disabled={selectedReport.meta.id === undefined || isHistoryTrendUnavailable}
                    className={isHistoryTrendOpen ? 'border-primary/70 bg-primary/15 text-primary shadow-glow-cyan' : undefined}
                    onClick={() => {
                      if (isHistoryTrendOpen) {
                        closeHistoryTrend();
                        return;
                      }
                      void openHistoryTrend();
                    }}
                  >
                    <BarChart3 className="h-4 w-4" />
                    {t('home.historyTrend')}
                  </Button>
                  <Button
                    variant="home-action-ai"
                    size="sm"
                    disabled={selectedReport.meta.id === undefined}
                    onClick={openMarkdownDrawer}
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    {t('home.fullReport')}
                  </Button>
                </div>
                {isHistoryTrendOpen ? (
                  <StockHistoryTrendDrawer
                    key={`stock-history-${selectedReport.meta.id}`}
                    report={selectedReport}
                    items={stockHistoryItems}
                    total={stockHistoryTotal}
                    hasMore={stockHistoryHasMore}
                    isLoading={isLoadingStockHistory}
                    isLoadingMore={isLoadingMoreStockHistory}
                    error={stockHistoryError}
                    filters={stockHistoryFilters}
                    onClose={closeHistoryTrend}
                    onRangeChange={(range) => void setStockHistoryRange(range)}
                    onLoadMore={() => void loadMoreStockHistory()}
                    onSelectRecord={(recordId) => void selectHistoryItem(recordId)}
                    onRetry={() => void openHistoryTrend()}
                  />
                ) : (
                  <ReportSummary
                    data={selectedReport}
                    isHistory
                    onOpenRunFlow={openHistoryRunFlow}
                    watchlist={{
                      isInWatchlist: watchlistState.isInWatchlist,
                      onToggle: watchlistState.toggleWatchlist,
                      isActioning: watchlistState.isActioning,
                      actionMessage: watchlistState.actionMessage,
                    }}
                  />
                )}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center">
                <EmptyState
                  title={t('home.startAnalysisTitle')}
                  description={t('home.startAnalysisDescription')}
                  className="max-w-xl border-dashed"
                  icon={(
                    <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                    </svg>
                  )}
                />
              </div>
            )}
          </section>
        </div>
      </div>

      {markdownDrawerOpen && selectedReport?.meta.id ? (
        <ReportMarkdownDrawer
          key={selectedReport.meta.id}
          recordId={selectedReport.meta.id}
          stockName={selectedReport.meta.stockName || ''}
          stockCode={selectedReport.meta.stockCode}
          reportLanguage={reportLanguage}
          onClose={closeMarkdownDrawer}
        />
      ) : null}

      {runFlowDrawer.open ? (
        <Drawer
          isOpen={runFlowDrawer.open}
          onClose={closeRunFlowDrawer}
          title={t('runFlow.drawerTitle')}
          width="max-w-[96vw]"
          zIndex={80}
        >
          <RunFlowPanel
            key={`${runFlowDrawer.source.type}-${runFlowDrawer.source.type === 'task' ? runFlowDrawer.source.taskId : runFlowDrawer.source.recordId}`}
            source={runFlowDrawer.source}
            title={runFlowDrawer.title}
          />
        </Drawer>
      ) : null}

    </div>
  );
};

export default HomePage;
