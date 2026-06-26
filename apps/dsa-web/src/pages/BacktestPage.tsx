import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { FileText, Play, RefreshCw, Trash2 } from 'lucide-react';
import { backtestApi } from '../api/backtest';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { historyApi } from '../api/history';
import { ApiErrorAlert, Badge, Card, EmptyState, Pagination } from '../components/common';
import type {
  BacktestRunResponse,
  CryptoBacktestHistoryItem,
  CryptoBacktestHistoryPlan,
  PerformanceMetrics,
} from '../types/backtest';

const INPUT_CLASS =
  'input-surface input-focus-glow h-11 rounded-xl border bg-transparent px-4 text-sm transition-all focus:outline-none disabled:cursor-not-allowed disabled:opacity-60';
const PAGE_SIZE = 20;

function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(2)}%`;
}

function money(value: unknown): string {
  if (typeof value !== 'number') return '--';
  return value.toFixed(2);
}

function planTypeLabel(value: string): string {
  const labels: Record<string, string> = {
    daily_long: '日线多单',
    daily_short: '日线空单',
    intraday: '小时线日内',
  };
  return labels[value] ?? value;
}

function directionLabel(value: string): string {
  const labels: Record<string, string> = {
    long: '做多',
    short: '做空',
    wait: '观望',
  };
  return labels[value] ?? value;
}

function statusBadge(status: string): React.ReactNode {
  const normalized = status || 'pending';
  if (['win', 'completed'].includes(normalized)) return <Badge variant="success">{normalized === 'win' ? '盈利' : '已回测'}</Badge>;
  if (normalized === 'loss') return <Badge variant="danger">亏损</Badge>;
  if (['neutral', 'no_entry'].includes(normalized)) return <Badge variant="warning">{normalized === 'no_entry' ? '未触发' : '持平'}</Badge>;
  if (normalized === 'pending') return <Badge variant="default">待回测</Badge>;
  if (normalized === 'invalid_plan') return <Badge variant="danger">计划缺字段</Badge>;
  if (normalized === 'no_plan') return <Badge variant="default">无计划</Badge>;
  if (normalized === 'partial') return <Badge variant="warning">部分回测</Badge>;
  if (normalized === 'insufficient_data') return <Badge variant="warning">样本不足</Badge>;
  return <Badge variant="default">{normalized}</Badge>;
}

const RunSummary: React.FC<{ data: BacktestRunResponse }> = ({ data }) => (
  <div className="backtest-summary animate-fade-in">
    <span className="label">记录 <span className="value">{data.processed}</span></span>
    <span className="label">写入 <span className="value primary">{data.saved}</span></span>
    <span className="label">完成 <span className="value success">{data.completed}</span></span>
    <span className="label">样本不足 <span className="value warning">{data.insufficient}</span></span>
    {data.skipped ? <span className="label">跳过 <span className="value">{data.skipped}</span></span> : null}
    {data.errors > 0 ? <span className="label">错误 <span className="value danger">{data.errors}</span></span> : null}
  </div>
);

const MetricRow: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="backtest-metric-row">
    <span className="label">{label}</span>
    <span className="value">{value}</span>
  </div>
);

const PerformanceCard: React.FC<{ metrics: PerformanceMetrics | null }> = ({ metrics }) => {
  if (!metrics) {
    return (
      <EmptyState
        title="暂无回测指标"
        description="选择历史分析记录并执行回测后，这里会展示 BTC 计划表现。"
        className="h-full min-h-[12rem] border-dashed bg-card/45 shadow-none"
      />
    );
  }

  return (
    <Card variant="gradient" padding="md">
      <div className="mb-3">
        <span className="label-uppercase">BTC 计划表现</span>
      </div>
      <MetricRow label="胜率" value={pct(metrics.winRatePct)} />
      <MetricRow label="方向准确率" value={pct(metrics.directionAccuracyPct)} />
      <MetricRow label="平均净收益" value={pct(metrics.avgSimulatedReturnPct)} />
      <MetricRow label="已触发 / 总样本" value={`${metrics.triggeredCount ?? 0} / ${metrics.totalEvaluations}`} />
      <MetricRow label="盈利 / 亏损 / 持平" value={`${metrics.winCount} / ${metrics.lossCount} / ${metrics.neutralCount}`} />
    </Card>
  );
};

const PlanSummary: React.FC<{
  plan: CryptoBacktestHistoryPlan;
  runningKey: string | null;
  onRun: (plan: CryptoBacktestHistoryPlan) => void;
}> = ({ plan, runningKey, onRun }) => {
  const latest = plan.latestResult;
  const trade = latest?.trade ?? {};
  const key = plan.planType;
  return (
    <div className="rounded-lg border border-white/10 bg-elevated/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={plan.horizon === 'intraday' ? 'warning' : 'info'}>{planTypeLabel(plan.planType)}</Badge>
        <Badge variant={plan.direction === 'short' ? 'danger' : plan.direction === 'long' ? 'success' : 'default'}>
          {directionLabel(plan.direction)}
        </Badge>
        {statusBadge(plan.backtestStatus)}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-secondary-text sm:grid-cols-4">
        <span>入场 {plan.entryPrice ?? '--'}</span>
        <span>止损 {plan.stopLoss ?? '--'}</span>
        <span>止盈 {plan.takeProfit ?? '--'}</span>
        <span>风报比 {plan.riskReward || money(trade.rMultiple)}</span>
      </div>
      {(plan.positionHint || plan.confidence || plan.invalidCondition) ? (
        <div className="mt-2 grid grid-cols-1 gap-1 text-xs text-muted-text">
          {plan.positionHint ? <span>仓位 {plan.positionHint}</span> : null}
          {plan.confidence ? <span>置信 {plan.confidence}</span> : null}
          {plan.invalidCondition ? <span>失效 {plan.invalidCondition}</span> : null}
        </div>
      ) : null}
      {latest ? (
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-muted-text sm:grid-cols-4">
          <span>触发 {latest.entryTriggered ? '是' : '否'}</span>
          <span>入场价 {latest.entryPrice ?? '--'}</span>
          <span>净收益 {pct(latest.simulatedReturnPct)}</span>
          <span>净 PnL {money(trade.netPnl)}</span>
        </div>
      ) : null}
      {!plan.backtestable ? (
        <p className="mt-2 text-xs text-danger">
          缺少关键字段：{plan.missingFields.join('、') || plan.noTradeReason || '不可回测'}
        </p>
      ) : null}
      <div className="mt-3 flex justify-end">
        <button
          type="button"
          disabled={!plan.backtestable || runningKey === key}
          onClick={() => onRun(plan)}
          className="btn-secondary inline-flex items-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Play className="h-3.5 w-3.5" />
          {runningKey === key ? '回测中' : '回测计划'}
        </button>
      </div>
    </div>
  );
};

const BacktestPage: React.FC = () => {
  const [codeFilter, setCodeFilter] = useState('BTC');
  const [history, setHistory] = useState<CryptoBacktestHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [metrics, setMetrics] = useState<PerformanceMetrics | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isRunningBatch, setIsRunningBatch] = useState(false);
  const [runningRecordId, setRunningRecordId] = useState<number | null>(null);
  const [runningPlanKey, setRunningPlanKey] = useState<string | null>(null);
  const [deletingIds, setDeletingIds] = useState(false);
  const [forceRerun, setForceRerun] = useState(false);
  const [runResult, setRunResult] = useState<BacktestRunResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);

  useEffect(() => {
    document.title = 'BTC 回测 - DSA';
  }, []);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const visibleIds = history.map((item) => item.analysisHistoryId);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedSet.has(id));

  const fetchData = useCallback(async (page = currentPage) => {
    setIsLoading(true);
    try {
      const [historyResponse, performance] = await Promise.all([
        backtestApi.getHistory({
          code: codeFilter.trim() || 'BTC',
          page,
          limit: PAGE_SIZE,
        }),
        backtestApi.getOverallPerformance(),
      ]);
      setHistory(historyResponse.items);
      setTotal(historyResponse.total);
      setCurrentPage(historyResponse.page);
      setMetrics(performance);
      setError(null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, [codeFilter, currentPage]);

  useEffect(() => {
    void fetchData(1);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshCurrentPage = async () => {
    await fetchData(currentPage);
  };

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => (
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    ));
  };

  const toggleSelectVisible = () => {
    setSelectedIds((prev) => {
      const current = new Set(prev);
      if (allVisibleSelected) {
        visibleIds.forEach((id) => current.delete(id));
      } else {
        visibleIds.forEach((id) => current.add(id));
      }
      return Array.from(current);
    });
  };

  const runForIds = async (ids: number[], planTypes?: string[]) => {
    if (!ids.length) return;
    setRunResult(null);
    setError(null);
    const singleId = ids.length === 1 ? ids[0] : null;
    if (singleId) setRunningRecordId(singleId);
    else setIsRunningBatch(true);
    try {
      const result = await backtestApi.runSelected({
        analysisHistoryIds: ids,
        planTypes,
        force: forceRerun,
      });
      setRunResult(result);
      await refreshCurrentPage();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRunningRecordId(null);
      setRunningPlanKey(null);
      setIsRunningBatch(false);
    }
  };

  const deleteSelected = async () => {
    if (!selectedIds.length) return;
    const confirmed = window.confirm(`确认删除 ${selectedIds.length} 条历史分析记录？对应报告入口和回测追溯也会受影响。`);
    if (!confirmed) return;
    setDeletingIds(true);
    setError(null);
    try {
      await historyApi.deleteRecords(selectedIds);
      setSelectedIds([]);
      await fetchData(history.length === selectedIds.length && currentPage > 1 ? currentPage - 1 : currentPage);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setDeletingIds(false);
    }
  };

  return (
    <div className="min-h-full flex flex-col rounded-[1.5rem] bg-transparent">
      <header className="flex-shrink-0 border-b border-white/5 px-3 py-3 sm:px-4">
        <div className="flex max-w-6xl flex-wrap items-center gap-2">
          <input
            type="text"
            value={codeFilter}
            onChange={(event) => setCodeFilter(event.target.value.toUpperCase())}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void fetchData(1);
            }}
            placeholder="BTC"
            className={`${INPUT_CLASS} w-44`}
          />
          <button
            type="button"
            onClick={() => void fetchData(1)}
            disabled={isLoading}
            className="btn-secondary inline-flex items-center gap-1.5 whitespace-nowrap"
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </button>
          <button
            type="button"
            onClick={() => setForceRerun(!forceRerun)}
            className={`backtest-force-btn ${forceRerun ? 'active' : ''}`}
          >
            <span className="dot" />
            强制重算
          </button>
          <button
            type="button"
            onClick={() => void runForIds(selectedIds)}
            disabled={!selectedIds.length || isRunningBatch}
            className="btn-primary inline-flex items-center gap-1.5 whitespace-nowrap disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Play className="h-4 w-4" />
            {isRunningBatch ? '批量回测中' : `批量回测 ${selectedIds.length || ''}`}
          </button>
          <button
            type="button"
            onClick={() => void deleteSelected()}
            disabled={!selectedIds.length || deletingIds}
            className="btn-secondary inline-flex items-center gap-1.5 whitespace-nowrap text-danger disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Trash2 className="h-4 w-4" />
            {deletingIds ? '删除中' : '批量删除'}
          </button>
        </div>
        <p className="mt-2 text-xs text-muted-text">
          回测页现在以 BTC 历史分析记录为主对象，可按报告选择计划回测，并查看入场触发、收益、PnL 和状态摘要。
        </p>
        {runResult ? (
          <div className="mt-2 max-w-4xl">
            <RunSummary data={runResult} />
          </div>
        ) : null}
      </header>

      <main className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3 lg:flex-row">
        <aside className="flex max-h-[38vh] flex-col gap-3 overflow-y-auto lg:max-h-none lg:w-64 lg:flex-shrink-0">
          <PerformanceCard metrics={metrics} />
          <Card padding="md" className="text-sm">
            <span className="label-uppercase">选择</span>
            <div className="mt-3 flex items-center justify-between text-secondary-text">
              <span>已选择</span>
              <span className="font-mono text-foreground">{selectedIds.length}</span>
            </div>
            <button
              type="button"
              onClick={toggleSelectVisible}
              disabled={!history.length}
              className="btn-secondary mt-3 w-full justify-center text-xs disabled:cursor-not-allowed disabled:opacity-60"
            >
              {allVisibleSelected ? '取消当前页' : '全选当前页'}
            </button>
          </Card>
        </aside>

        <section className="min-h-0 flex-1 overflow-y-auto">
          {error ? <ApiErrorAlert error={error} className="mb-3" /> : null}
          {isLoading ? (
            <div className="flex h-64 flex-col items-center justify-center">
              <div className="backtest-spinner md" />
              <p className="mt-3 text-sm text-secondary-text">加载 BTC 历史分析记录...</p>
            </div>
          ) : history.length === 0 ? (
            <EmptyState
              title="暂无 BTC 历史分析记录"
              description="先运行一次 BTC 分析，回测页会自动把可验证计划列出来。"
              icon={<FileText className="h-6 w-6" />}
            />
          ) : (
            <div className="animate-fade-in">
              <div className="backtest-table-toolbar">
                <div className="backtest-table-toolbar-meta">
                  <span className="label-uppercase">历史分析记录</span>
                  <span className="text-xs text-secondary-text">共 {total} 条 · 第 {currentPage} 页</span>
                </div>
                <span className="backtest-table-scroll-hint">可横向滚动查看计划摘要</span>
              </div>

              <div className="flex flex-col gap-3">
                {history.map((item) => {
                  const isSelected = selectedSet.has(item.analysisHistoryId);
                  const backtestablePlans = item.plans.filter((plan) => plan.backtestable);
                  return (
                    <div
                      key={item.analysisHistoryId}
                      className="rounded-xl border border-white/10 bg-card/70 p-4 shadow-soft-card"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <label className="flex min-w-0 items-start gap-3">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleSelect(item.analysisHistoryId)}
                            className="mt-1 h-4 w-4 rounded border-border bg-elevated"
                          />
                          <span className="min-w-0">
                            <span className="flex flex-wrap items-center gap-2">
                              <span className="font-mono text-sm text-foreground">{item.code}</span>
                              <Badge variant="history">#{item.analysisHistoryId}</Badge>
                              <Badge variant={item.analysisMode === 'hourly' ? 'warning' : 'default'}>
                                {item.analysisTimeframe || item.analysisMode || '日线'}
                              </Badge>
                              {statusBadge(item.backtestStatus)}
                            </span>
                            <span className="mt-1 block text-xs text-muted-text">
                              {item.analysisCreatedAt || '--'} · {item.stockName || 'Bitcoin'}
                            </span>
                            <span className="mt-2 block max-w-3xl truncate text-sm text-secondary-text">
                              {item.analysisSummary || item.trendPrediction || item.operationAdvice || '无摘要'}
                            </span>
                          </span>
                        </label>
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={`/?history=${item.analysisHistoryId}`}
                            className="btn-secondary inline-flex items-center gap-1.5 text-xs"
                          >
                            <FileText className="h-3.5 w-3.5" />
                            报告
                          </Link>
                          <button
                            type="button"
                            disabled={!backtestablePlans.length || runningRecordId === item.analysisHistoryId}
                            onClick={() => void runForIds([item.analysisHistoryId])}
                            className="btn-primary inline-flex items-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {runningRecordId === item.analysisHistoryId ? (
                              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <Play className="h-3.5 w-3.5" />
                            )}
                            回测记录
                          </button>
                        </div>
                      </div>

                      <div className="mt-3 grid gap-2 xl:grid-cols-3">
                        {item.plans.length ? item.plans.map((plan) => (
                          <PlanSummary
                            key={`${item.analysisHistoryId}:${plan.planType}`}
                            plan={plan}
                            runningKey={runningRecordId === item.analysisHistoryId ? runningPlanKey : null}
                            onRun={(selectedPlan) => {
                              setRunningRecordId(item.analysisHistoryId);
                              setRunningPlanKey(selectedPlan.planType);
                              void runForIds([item.analysisHistoryId], [selectedPlan.planType]);
                            }}
                          />
                        )) : (
                          <div className="rounded-lg border border-dashed border-white/10 p-3 text-sm text-muted-text">
                            该报告没有可解析的结构化交易计划。
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="mt-4">
                <Pagination
                  currentPage={currentPage}
                  totalPages={Math.max(totalPages, 1)}
                  onPageChange={(page) => void fetchData(page)}
                />
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
};

export default BacktestPage;
