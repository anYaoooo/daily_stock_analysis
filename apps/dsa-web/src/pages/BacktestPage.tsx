import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Eye, FileText, Play, RefreshCw, Trash2 } from 'lucide-react';
import { backtestApi } from '../api/backtest';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { historyApi } from '../api/history';
import { ApiErrorAlert, Badge, Card, Drawer, EmptyState, Pagination } from '../components/common';
import type {
  BacktestRunResponse,
  BacktestTimeframeFilter,
  CryptoBacktestDirectionFilter,
  CryptoBacktestHistoryItem,
  CryptoBacktestHistoryPlan,
  CryptoBacktestLossReviewResponse,
  CryptoBacktestPlanTypeFilter,
  CryptoBacktestResultStatusFilter,
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

function formatAnalysisTime(value?: string): string {
  if (!value) return '--';
  return value.replace('T', ' ').slice(0, 16);
}

function planResultSummary(plans: CryptoBacktestHistoryPlan[]): string {
  if (!plans.length) return '无计划';
  const labels: Record<string, string> = {
    win: '盈利',
    loss: '亏损',
    neutral: '持平',
    no_entry: '未触发',
    signal_rejected: '信号触发后拒单',
    pending: '待回测',
    insufficient_data: '等待数据',
    invalid_plan: '不可评估',
    skipped: '跳过',
  };
  const counts = new Map<string, number>();
  plans.forEach((plan) => {
    const status = plan.latestResult?.orderStatus === 'rejected'
      ? 'signal_rejected'
      : plan.latestResult?.outcome || plan.backtestStatus || 'pending';
    counts.set(status, (counts.get(status) ?? 0) + 1);
  });
  return Array.from(counts, ([status, count]) => `${labels[status] ?? status} ${count}`).join(' · ');
}

function statusBadge(status: string): React.ReactNode {
  const normalized = status || 'pending';
  if (['win', 'completed'].includes(normalized)) return <Badge variant="success">{normalized === 'win' ? '盈利' : '已回测'}</Badge>;
  if (normalized === 'loss') return <Badge variant="danger">亏损</Badge>;
  if (normalized === 'signal_rejected') return <Badge variant="warning">信号触发后拒单</Badge>;
  if (['neutral', 'no_entry'].includes(normalized)) return <Badge variant="warning">{normalized === 'no_entry' ? '未触发' : '持平'}</Badge>;
  if (normalized === 'pending') return <Badge variant="default">待回测</Badge>;
  if (normalized === 'skipped') return <Badge variant="default">不计入样本</Badge>;
  if (normalized === 'invalid_plan') return <Badge variant="danger">计划缺字段</Badge>;
  if (normalized === 'no_plan') return <Badge variant="default">无计划</Badge>;
  if (normalized === 'partial') return <Badge variant="warning">部分回测</Badge>;
  if (normalized === 'insufficient_data') return <Badge variant="warning">等待评估数据</Badge>;
  return <Badge variant="default">{normalized}</Badge>;
}

function orderStatusLabel(value?: string): string {
  const labels: Record<string, string> = {
    filled: '已成交',
    rejected: '已拒单',
    pending_fill: '等待成交',
    not_triggered: '未生成委托',
    not_evaluated: '待评估',
    not_applicable: '不适用',
  };
  return labels[value || ''] ?? value ?? '--';
}

function tagValue(tags: Record<string, unknown> | null | undefined, group: string, key: string): string | null {
  const section = tags?.[group];
  if (!section || typeof section !== 'object') return null;
  const value = (section as Record<string, unknown>)[key];
  return typeof value === 'string' && value ? value : null;
}

const RunSummary: React.FC<{ data: BacktestRunResponse }> = ({ data }) => (
  <div className="backtest-summary animate-fade-in">
    <span className="label">记录 <span className="value">{data.processed}</span></span>
    <span className="label">写入 <span className="value primary">{data.saved}</span></span>
    <span className="label">完成 <span className="value success">{data.completed}</span></span>
    <span className="label">等待评估数据 <span className="value warning">{data.insufficient}</span></span>
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

  const sampleConfidence = metrics.diagnostics?.sampleConfidence as
    | { isLowConfidence?: boolean; sampleCount?: number; minimumSampleCount?: number }
    | undefined;
  const metricSemantics = metrics.diagnostics?.metricSemantics;
  const contractMetrics = metricSemantics === 'structured_execution_contract';
  const rawTriggeredCount = metrics.diagnostics?.rawTriggeredCount;
  const overlapExcludedCount = metrics.diagnostics?.overlapExcludedCount;
  const signalTriggeredCount = metrics.diagnostics?.signalTriggeredCount;
  const rejectedOrderCount = metrics.diagnostics?.rejectedOrderCount;
  const orderFillRatePct = metrics.diagnostics?.orderFillRatePct;
  const avgMissedFavorableMovePct = metrics.diagnostics?.avgMissedFavorableMovePct;

  return (
    <Card variant="gradient" padding="md">
      <div className="mb-3 flex items-center justify-between gap-2">
        <span className="label-uppercase">BTC 计划表现</span>
        {sampleConfidence?.isLowConfidence ? <Badge variant="warning">低样本</Badge> : null}
      </div>
      <MetricRow label={contractMetrics ? '策略契约胜率' : '触价代理胜率'} value={pct(metrics.winRatePct)} />
      <MetricRow label="方向准确率" value={pct(metrics.directionAccuracyPct)} />
      <MetricRow label="平均净收益" value={pct(metrics.avgSimulatedReturnPct)} />
      {typeof signalTriggeredCount === 'number' && typeof rawTriggeredCount === 'number' ? (
        <MetricRow label="信号 / 实际成交 / 拒单" value={`${signalTriggeredCount} / ${rawTriggeredCount} / ${typeof rejectedOrderCount === 'number' ? rejectedOrderCount : 0}`} />
      ) : null}
      {typeof orderFillRatePct === 'number' ? <MetricRow label="信号成交率" value={pct(orderFillRatePct)} /> : null}
      {typeof avgMissedFavorableMovePct === 'number' ? <MetricRow label="拒单后平均有利波动" value={pct(avgMissedFavorableMovePct)} /> : null}
      <MetricRow label="独立成交（收益统计）/ 已完成评估" value={`${metrics.triggeredCount ?? 0} / ${metrics.completedCount}`} />
      <MetricRow label="不可评估 / 等待数据" value={`${metrics.skippedCount ?? 0} / ${metrics.insufficientCount ?? 0}`} />
      {typeof rawTriggeredCount === 'number' && typeof overlapExcludedCount === 'number' ? (
        <MetricRow label="重叠持仓排除" value={`${overlapExcludedCount}`} />
      ) : null}
      <MetricRow label="盈利 / 亏损 / 持平" value={`${metrics.winCount} / ${metrics.lossCount} / ${metrics.neutralCount}`} />
      {sampleConfidence?.minimumSampleCount ? (
        <MetricRow label="可信样本门槛" value={`${sampleConfidence.sampleCount ?? 0} / ${sampleConfidence.minimumSampleCount}`} />
      ) : null}
    </Card>
  );
};

type IndicatorBucket = {
  dimension: string;
  dimensionLabel?: string;
  key: string;
  totalEvaluations: number;
  completedCount?: number;
  triggeredCount?: number;
  winRatePct?: number | null;
  avgSimulatedReturnPct?: number | null;
  maxDrawdownPct?: number | null;
  avgRMultiple?: number | null;
  sampleConfidence?: {
    isLowConfidence?: boolean;
  };
};

function indicatorBuckets(metrics: PerformanceMetrics | null): IndicatorBucket[] {
  const breakdown = metrics?.diagnostics?.indicatorGroupBreakdown;
  if (!breakdown || typeof breakdown !== 'object') return [];
  const groups = (breakdown as { groups?: Record<string, unknown> }).groups;
  if (!groups || typeof groups !== 'object') return [];
  const preferred = [
    ['planType', 'plan_type'],
    ['direction'],
    ['intradayAlignment', 'intraday.alignment'],
    ['priceActionState', 'price_action.state'],
    ['vwapPricePosition', 'vwap.price_position'],
    ['emaStructure', 'ema.structure'],
    ['volumeConfirmation', 'volume.confirmation'],
    ['eventType', 'event.type'],
  ];
  return preferred.flatMap((keys) => {
    const items = keys.map((key) => groups[key]).find((value) => Array.isArray(value));
    if (!Array.isArray(items)) return [];
    return items as IndicatorBucket[];
  }).filter((item) => item.key !== 'unknown').slice(0, 8);
}

const IndicatorGroupCard: React.FC<{ metrics: PerformanceMetrics | null }> = ({ metrics }) => {
  const buckets = indicatorBuckets(metrics);
  if (!buckets.length) {
    return null;
  }

  return (
    <Card padding="md" className="text-sm">
      <span className="label-uppercase">指标分组复盘</span>
      <div className="mt-3 flex flex-col gap-2">
        {buckets.map((bucket) => (
          <div key={`${bucket.dimension}:${bucket.key}`} className="rounded-lg border border-white/10 bg-elevated/35 p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-xs text-secondary-text">
                {bucket.dimensionLabel || bucket.dimension} · {bucket.key}
              </span>
              {bucket.sampleConfidence?.isLowConfidence ? <Badge variant="warning">低样本</Badge> : null}
            </div>
            <div className="mt-1 grid grid-cols-2 gap-1 text-xs text-muted-text">
              <span>已完成 {bucket.completedCount ?? 0}</span>
              <span>触发 {bucket.triggeredCount ?? 0}</span>
              <span>胜率 {pct(bucket.winRatePct)}</span>
              <span>均收益 {pct(bucket.avgSimulatedReturnPct)}</span>
              <span>回撤 {pct(bucket.maxDrawdownPct)}</span>
              <span>均 R {money(bucket.avgRMultiple)}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
};

const LossReviewCard: React.FC<{ review: CryptoBacktestLossReviewResponse | null }> = ({ review }) => {
  if (!review) return null;

  return (
    <Card padding="md" className="text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="label-uppercase">亏损复盘</span>
        <Badge variant={review.lossCount ? 'danger' : 'default'}>{review.lossCount} 笔</Badge>
      </div>
      <p className="mt-2 text-xs text-muted-text">当前引擎 {review.engineVersion} · 已复盘 {review.reviewedResults} 笔净亏损</p>
      {!review.items.length ? (
        <p className="mt-3 text-xs text-secondary-text">暂无可归因的净亏损成交，后续回测会自动纳入复盘。</p>
      ) : (
        <div className="mt-3 divide-y divide-border/60">
          {review.items.slice(0, 3).map((item) => (
            <div key={`${item.analysisHistoryId}-${item.planType}`} className="py-3 first:pt-0 last:pb-0">
              <div className="flex items-start justify-between gap-2">
                <span className="text-xs font-medium text-foreground">{item.title}</span>
                <Badge variant={item.causeGroup === 'execution' ? 'warning' : 'danger'}>{pct(item.simulatedReturnPct)}</Badge>
              </div>
              <p className="mt-1 text-xs text-secondary-text">{item.explanation}</p>
              <p className="mt-1 text-xs text-muted-text">改进：{item.improvement}</p>
            </div>
          ))}
        </div>
      )}
      {review.indicatorPatterns.length ? (
        <div className="mt-3 border-t border-border/60 pt-3 text-xs text-muted-text">
          共同特征：{review.indicatorPatterns.map((item) => `${item.dimension}.${item.key} (${item.lossCount})`).join(' · ')}
        </div>
      ) : null}
      {review.improvementSuggestions.length ? (
        <ul className="mt-3 space-y-1 border-t border-border/60 pt-3 text-xs text-secondary-text">
          {review.improvementSuggestions.slice(0, 2).map((suggestion) => <li key={suggestion}>{suggestion}</li>)}
        </ul>
      ) : null}
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
        {plan.setupType ? (
          <Badge variant="default">{plan.setupType === 'pullback' ? '回踩' : plan.setupType === 'breakout' ? '突破' : plan.setupType}</Badge>
        ) : null}
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
      {plan.executionContract ? (
        <div className="mt-2"><Badge variant="info">结构化执行契约</Badge></div>
      ) : null}
      {(plan.positionHint || plan.confidence || plan.invalidCondition) ? (
        <div className="mt-2 grid grid-cols-1 gap-1 text-xs text-muted-text">
          {plan.positionHint ? <span>仓位 {plan.positionHint}</span> : null}
          {plan.confidence ? <span>置信 {plan.confidence}</span> : null}
          {plan.invalidCondition ? <span>失效 {plan.invalidCondition}</span> : null}
        </div>
      ) : null}
      {latest ? (
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-muted-text sm:grid-cols-4">
          <span>信号 {latest.signalTriggered ? '已触发' : '未触发'}</span>
          <span>委托 {orderStatusLabel(latest.orderStatus)}</span>
          <span>成交 {latest.entryTriggered ? '是' : '否'}</span>
          <span>入场价 {latest.entryPrice ?? '--'}</span>
          <span>净收益 {pct(latest.simulatedReturnPct)}</span>
          <span>净 PnL {money(trade.netPnl)}</span>
          <span>错失有利波动 {pct(latest.missedFavorableMovePct)}</span>
          <span>拒单后不利波动 {pct(latest.missedAdverseMovePct)}</span>
        </div>
      ) : null}
      {latest?.orderStatus === 'rejected' ? (
        <p className="mt-2 text-xs text-warning">
          信号已经成立，但实际成交价未通过风控：{latest.orderRejectionReason || latest.simulatedExitReason || '成交质量不合格'}
        </p>
      ) : null}
      {plan.indicatorTags ? (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-muted-text">
          {tagValue(plan.indicatorTags, 'priceAction', 'state') ? <span>PA {tagValue(plan.indicatorTags, 'priceAction', 'state')}</span> : null}
          {tagValue(plan.indicatorTags, 'ema', 'structure') ? <span>EMA {tagValue(plan.indicatorTags, 'ema', 'structure')}</span> : null}
          {tagValue(plan.indicatorTags, 'vwap', 'pricePosition') ? <span>VWAP {tagValue(plan.indicatorTags, 'vwap', 'pricePosition')}</span> : null}
          {tagValue(plan.indicatorTags, 'volume', 'confirmation') ? <span>量能 {tagValue(plan.indicatorTags, 'volume', 'confirmation')}</span> : null}
          {tagValue(plan.indicatorTags, 'intraday', 'alignment') ? <span>对齐 {tagValue(plan.indicatorTags, 'intraday', 'alignment')}</span> : null}
          {tagValue(plan.indicatorTags, 'event', 'type') ? <span>事件 {tagValue(plan.indicatorTags, 'event', 'type')}</span> : null}
        </div>
      ) : null}
      {!plan.backtestable ? (
        <p className={`mt-2 text-xs ${plan.qualityStatus === 'no_trade_plan' ? 'text-muted-text' : 'text-danger'}`}>
          {plan.qualityStatus === 'no_trade_plan'
            ? `观望计划，不计入有效样本${plan.noTradeReason ? `：${plan.noTradeReason}` : ''}`
            : plan.missingFields.length === 1 && plan.missingFields[0] === 'execution_contract'
            ? '旧报告没有 v3 执行契约，不计入有效样本'
            : `缺少关键字段：${plan.missingFields.join('、') || plan.noTradeReason || '不可回测'}`}
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
  const [analysisModeFilter, setAnalysisModeFilter] = useState<BacktestTimeframeFilter>('all');
  const [directionFilter, setDirectionFilter] = useState<CryptoBacktestDirectionFilter>('all');
  const [planTypeFilter, setPlanTypeFilter] = useState<CryptoBacktestPlanTypeFilter>('all');
  const [resultStatusFilter, setResultStatusFilter] = useState<CryptoBacktestResultStatusFilter>('all');
  const [history, setHistory] = useState<CryptoBacktestHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [metrics, setMetrics] = useState<PerformanceMetrics | null>(null);
  const [dailyMetrics, setDailyMetrics] = useState<PerformanceMetrics | null>(null);
  const [intradayMetrics, setIntradayMetrics] = useState<PerformanceMetrics | null>(null);
  const [lossReview, setLossReview] = useState<CryptoBacktestLossReviewResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isRunningBatch, setIsRunningBatch] = useState(false);
  const [runningRecordId, setRunningRecordId] = useState<number | null>(null);
  const [runningPlanKey, setRunningPlanKey] = useState<string | null>(null);
  const [deletingIds, setDeletingIds] = useState(false);
  const [forceRerun, setForceRerun] = useState(false);
  const [runResult, setRunResult] = useState<BacktestRunResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [detailItem, setDetailItem] = useState<CryptoBacktestHistoryItem | null>(null);

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
      const [historyResponse, performance, review] = await Promise.all([
        backtestApi.getHistory({
          code: codeFilter.trim() || 'BTC',
          analysisMode: analysisModeFilter,
          direction: directionFilter,
          planType: planTypeFilter,
          resultStatus: resultStatusFilter,
          page,
          limit: PAGE_SIZE,
        }),
        backtestApi.getOverallPerformance({ analysisMode: analysisModeFilter }),
        backtestApi.getLossReview({ code: codeFilter.trim() || 'BTC' }).catch(() => null),
      ]);
      const [dailyPerformance, intradayPerformance] = await Promise.all([
        backtestApi.getOverallPerformance({ analysisMode: 'daily' }),
        backtestApi.getOverallPerformance({ analysisMode: 'hourly' }),
      ]);
      setHistory(historyResponse.items);
      setTotal(historyResponse.total);
      setCurrentPage(historyResponse.page);
      setMetrics(performance);
      setDailyMetrics(dailyPerformance);
      setIntradayMetrics(intradayPerformance);
      setLossReview(review);
      setError(null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, [analysisModeFilter, codeFilter, currentPage, directionFilter, planTypeFilter, resultStatusFilter]);

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
      const task = await backtestApi.runSelectedAsync({
        analysisHistoryIds: ids,
        planTypes,
        force: forceRerun,
      });
      let status = await backtestApi.getTask(task.taskId);
      while (status.status === 'pending' || status.status === 'processing') {
        await new Promise<void>((resolve) => window.setTimeout(resolve, 1000));
        status = await backtestApi.getTask(task.taskId);
      }
      if (status.status !== 'completed' || !status.result) {
        throw new Error(status.error || '回测任务未完成');
      }
      setRunResult(status.result);
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
          <select
            value={analysisModeFilter}
            onChange={(event) => setAnalysisModeFilter(event.target.value as BacktestTimeframeFilter)}
            className={`${INPUT_CLASS} w-36`}
            aria-label="分析模式"
          >
            <option value="all">全部模式</option>
            <option value="daily">日线</option>
            <option value="hourly">小时线</option>
          </select>
          <select
            value={directionFilter}
            onChange={(event) => setDirectionFilter(event.target.value as CryptoBacktestDirectionFilter)}
            className={`${INPUT_CLASS} w-32`}
            aria-label="方向"
          >
            <option value="all">全部方向</option>
            <option value="long">做多</option>
            <option value="short">做空</option>
            <option value="wait">观望</option>
          </select>
          <select
            value={planTypeFilter}
            onChange={(event) => setPlanTypeFilter(event.target.value as CryptoBacktestPlanTypeFilter)}
            className={`${INPUT_CLASS} w-40`}
            aria-label="计划类型"
          >
            <option value="all">全部计划</option>
            <option value="daily_long">日线多单</option>
            <option value="daily_short">日线空单</option>
            <option value="intraday">小时线日内</option>
          </select>
          <select
            value={resultStatusFilter}
            onChange={(event) => setResultStatusFilter(event.target.value as CryptoBacktestResultStatusFilter)}
            className={`${INPUT_CLASS} w-36`}
            aria-label="结果状态"
          >
            <option value="all">全部状态</option>
            <option value="pending">待回测</option>
            <option value="win">盈利</option>
            <option value="loss">亏损</option>
            <option value="neutral">持平</option>
            <option value="no_entry">未触发</option>
            <option value="insufficient_data">等待评估数据</option>
            <option value="invalid_plan">计划缺字段</option>
          </select>
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
          回测页以 BTC 历史分析记录为主对象，分别展示信号成立、委托状态、实际成交、收益和错失行情。
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
            <span className="label-uppercase">周期拆分</span>
            <div className="mt-3 grid gap-2 text-xs text-secondary-text">
              <div className="flex items-center justify-between">
                <span>日线主计划</span>
                <span>{dailyMetrics ? `${dailyMetrics.triggeredCount ?? 0}/${dailyMetrics.totalEvaluations} · ${pct(dailyMetrics.winRatePct)}` : '--'}</span>
              </div>
              <div className="flex items-center justify-between">
                <span>小时线日内</span>
                <span>{intradayMetrics ? `${intradayMetrics.triggeredCount ?? 0}/${intradayMetrics.totalEvaluations} · ${pct(intradayMetrics.winRatePct)}` : '--'}</span>
              </div>
            </div>
          </Card>
          <IndicatorGroupCard metrics={metrics} />
          <LossReviewCard review={lossReview} />
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

              <div className="overflow-x-auto rounded-lg border border-border/70 bg-card/55">
                <table className="w-full min-w-[1120px] text-left text-sm" aria-label="BTC 回测历史分析记录">
                  <thead className="border-b border-border/70 bg-elevated/45 text-xs text-muted-text">
                    <tr>
                      <th className="w-11 px-3 py-3">
                        <input
                          type="checkbox"
                          checked={allVisibleSelected}
                          onChange={toggleSelectVisible}
                          aria-label="全选当前页"
                          className="h-4 w-4 rounded border-border bg-elevated"
                        />
                      </th>
                      <th className="px-3 py-3 font-medium">分析时间</th>
                      <th className="px-3 py-3 font-medium">记录</th>
                      <th className="px-3 py-3 font-medium">周期</th>
                      <th className="min-w-72 px-3 py-3 font-medium">分析摘要</th>
                      <th className="min-w-56 px-3 py-3 font-medium">计划</th>
                      <th className="px-3 py-3 font-medium">状态</th>
                      <th className="min-w-40 px-3 py-3 font-medium">结果摘要</th>
                      <th className="px-3 py-3 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((item) => {
                      const isSelected = selectedSet.has(item.analysisHistoryId);
                      const backtestablePlans = item.plans.filter((plan) => plan.backtestable);
                      return (
                        <tr
                          key={item.analysisHistoryId}
                          className="border-b border-border/45 transition-colors hover:bg-hover/40 last:border-0"
                        >
                          <td className="px-3 py-3">
                            <input
                              type="checkbox"
                              checked={isSelected}
                              onChange={() => toggleSelect(item.analysisHistoryId)}
                              aria-label={`选择记录 #${item.analysisHistoryId}`}
                              className="h-4 w-4 rounded border-border bg-elevated"
                            />
                          </td>
                          <td className="whitespace-nowrap px-3 py-3 font-mono text-xs text-secondary-text">
                            {formatAnalysisTime(item.analysisCreatedAt)}
                          </td>
                          <td className="px-3 py-3">
                            <div className="font-mono font-medium text-foreground">{item.code}</div>
                            <div className="mt-1 text-xs text-muted-text">#{item.analysisHistoryId} · {item.stockName || 'Bitcoin'}</div>
                          </td>
                          <td className="px-3 py-3">
                            <Badge variant={item.analysisMode === 'hourly' ? 'warning' : 'default'}>
                              {item.analysisTimeframe || item.analysisMode || '日线'}
                            </Badge>
                          </td>
                          <td className="max-w-sm px-3 py-3 text-secondary-text">
                            <span className="line-clamp-2">{item.analysisSummary || item.trendPrediction || item.operationAdvice || '无摘要'}</span>
                          </td>
                          <td className="px-3 py-3">
                            <div className="flex flex-wrap gap-1">
                              {item.plans.length ? item.plans.map((plan) => (
                                <Badge key={plan.planType} variant={plan.direction === 'short' ? 'danger' : plan.direction === 'long' ? 'info' : 'default'}>
                                  {planTypeLabel(plan.planType)}
                                </Badge>
                              )) : <span className="text-xs text-muted-text">无计划</span>}
                            </div>
                          </td>
                          <td className="px-3 py-3">{statusBadge(item.backtestStatus)}</td>
                          <td className="px-3 py-3 text-xs text-secondary-text">{planResultSummary(item.plans)}</td>
                          <td className="px-3 py-3">
                            <div className="flex justify-end gap-2">
                              <button
                                type="button"
                                onClick={() => setDetailItem(item)}
                                className="btn-secondary inline-flex items-center gap-1.5 whitespace-nowrap text-xs"
                              >
                                <Eye className="h-3.5 w-3.5" />
                                详情
                              </button>
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
                                className="btn-primary inline-flex items-center gap-1.5 whitespace-nowrap text-xs disabled:cursor-not-allowed disabled:opacity-60"
                              >
                                {runningRecordId === item.analysisHistoryId ? (
                                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  <Play className="h-3.5 w-3.5" />
                                )}
                                回测
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
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

      <Drawer
        isOpen={Boolean(detailItem)}
        onClose={() => setDetailItem(null)}
        title={detailItem ? `${detailItem.code} · 回测详情` : undefined}
        width="max-w-4xl"
      >
        {detailItem ? (
          <div className="space-y-5">
            <section className="grid gap-3 rounded-lg border border-border/70 bg-elevated/35 p-4 sm:grid-cols-2">
              <div>
                <span className="label-uppercase">分析记录</span>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <span className="font-mono text-base font-semibold text-foreground">{detailItem.code}</span>
                  <Badge variant="history">#{detailItem.analysisHistoryId}</Badge>
                  {statusBadge(detailItem.backtestStatus)}
                </div>
                <p className="mt-2 text-sm text-secondary-text">{detailItem.analysisSummary || detailItem.trendPrediction || detailItem.operationAdvice || '无摘要'}</p>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <div><dt className="text-muted-text">分析时间</dt><dd className="mt-1 font-mono text-foreground">{formatAnalysisTime(detailItem.analysisCreatedAt)}</dd></div>
                <div><dt className="text-muted-text">分析周期</dt><dd className="mt-1 text-foreground">{detailItem.analysisTimeframe || detailItem.analysisMode || '日线'}</dd></div>
                <div><dt className="text-muted-text">计划结果</dt><dd className="mt-1 text-foreground">{planResultSummary(detailItem.plans)}</dd></div>
                <div><dt className="text-muted-text">可回测计划</dt><dd className="mt-1 text-foreground">{detailItem.plans.filter((plan) => plan.backtestable).length} / {detailItem.plans.length}</dd></div>
              </dl>
            </section>

            <div className="flex flex-wrap justify-end gap-2">
              <Link to={`/?history=${detailItem.analysisHistoryId}`} className="btn-secondary inline-flex items-center gap-1.5 text-xs">
                <FileText className="h-3.5 w-3.5" />
                查看报告
              </Link>
              <button
                type="button"
                disabled={!detailItem.plans.some((plan) => plan.backtestable) || runningRecordId === detailItem.analysisHistoryId}
                onClick={() => void runForIds([detailItem.analysisHistoryId])}
                className="btn-primary inline-flex items-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Play className="h-3.5 w-3.5" />
                回测全部计划
              </button>
            </div>

            <section>
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">计划明细</h3>
                <span className="text-xs text-muted-text">{detailItem.plans.length} 项</span>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {detailItem.plans.length ? detailItem.plans.map((plan) => (
                  <PlanSummary
                    key={`${detailItem.analysisHistoryId}:${plan.planType}`}
                    plan={plan}
                    runningKey={runningRecordId === detailItem.analysisHistoryId ? runningPlanKey : null}
                    onRun={(selectedPlan) => {
                      setRunningRecordId(detailItem.analysisHistoryId);
                      setRunningPlanKey(selectedPlan.planType);
                      void runForIds([detailItem.analysisHistoryId], [selectedPlan.planType]);
                    }}
                  />
                )) : (
                  <EmptyState title="没有结构化交易计划" description="这条分析记录没有可展示的回测计划。" />
                )}
              </div>
            </section>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
};

export default BacktestPage;
