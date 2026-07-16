import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { BarChart3, ExternalLink, RefreshCw } from 'lucide-react';
import { backtestApi } from '../../api/backtest';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import type { CryptoBacktestHistoryItem, CryptoBacktestHistoryPlan } from '../../types/backtest';
import type { ReportLanguage } from '../../types/analysis';
import { ApiErrorAlert, Badge, Card } from '../common';
import { DashboardPanelHeader, DashboardStateBlock } from '../dashboard';
import { normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportBacktestSummaryProps {
  recordId?: number;
  stockCode?: string;
  language?: ReportLanguage;
}

const TEXT = {
  zh: {
    eyebrow: '回测复盘',
    title: '计划级回测',
    loading: '加载回测状态...',
    emptyTitle: '暂无可回测计划',
    emptyDescription: '这份报告还没有结构化 BTC 交易计划，或计划缺少关键点位。',
    openBacktest: '回测页',
    refresh: '刷新',
    entry: '入场',
    stopLoss: '止损',
    takeProfit: '止盈',
    riskReward: '风报比',
    trigger: '触发',
    entryPrice: '入场价',
    netReturn: '净收益',
    netPnl: '净 PnL',
    rMultiple: 'R 倍数',
    missingFields: '缺少关键字段',
    legacyContract: '旧报告没有 v3 执行契约，不计入有效样本',
    noTradePlan: '观望计划，不计入有效样本',
    yes: '是',
    no: '否',
  },
  en: {
    eyebrow: 'Backtest review',
    title: 'Plan-level backtest',
    loading: 'Loading backtest status...',
    emptyTitle: 'No backtestable plan',
    emptyDescription: 'This report has no structured BTC plan yet, or required levels are missing.',
    openBacktest: 'Backtests',
    refresh: 'Refresh',
    entry: 'Entry',
    stopLoss: 'Stop',
    takeProfit: 'Target',
    riskReward: 'R/R',
    trigger: 'Triggered',
    entryPrice: 'Entry price',
    netReturn: 'Net return',
    netPnl: 'Net PnL',
    rMultiple: 'R multiple',
    missingFields: 'Missing fields',
    legacyContract: 'This legacy report has no v3 execution contract and is excluded from valid samples',
    noTradePlan: 'Wait plan, excluded from valid samples',
    yes: 'Yes',
    no: 'No',
  },
} as const;

function isBitcoinCode(value?: string): boolean {
  const code = (value || '').trim().toUpperCase();
  return ['BTC', 'BTCUSDT', 'BTCUSD', 'BTC-USD', 'BTC/USD', 'BTC_USDT'].includes(code);
}

function planTypeLabel(value: string, language: 'zh' | 'en'): string {
  const labels = {
    zh: {
      daily_long: '日线多单',
      daily_short: '日线空单',
      intraday: '小时线日内',
    },
    en: {
      daily_long: 'Daily long',
      daily_short: 'Daily short',
      intraday: 'Hourly intraday',
    },
  } as const;
  return labels[language][value as keyof typeof labels.zh] ?? value;
}

function directionLabel(value: string, language: 'zh' | 'en'): string {
  const labels = {
    zh: { long: '做多', short: '做空', wait: '观望' },
    en: { long: 'Long', short: 'Short', wait: 'Wait' },
  } as const;
  return labels[language][value as keyof typeof labels.zh] ?? value;
}

function statusBadge(status: string, language: 'zh' | 'en'): React.ReactNode {
  const normalized = status || 'pending';
  const labels = {
    zh: {
      win: '盈利',
      completed: '已回测',
      loss: '亏损',
      neutral: '持平',
      no_entry: '未触发',
      pending: '待回测',
      invalid_plan: '计划缺字段',
      no_plan: '无计划',
      partial: '部分回测',
      insufficient_data: '等待评估数据',
      skipped: '不计入样本',
    },
    en: {
      win: 'Win',
      completed: 'Tested',
      loss: 'Loss',
      neutral: 'Flat',
      no_entry: 'No entry',
      pending: 'Pending',
      invalid_plan: 'Missing fields',
      no_plan: 'No plan',
      partial: 'Partial',
      insufficient_data: 'Awaiting data',
      skipped: 'Excluded',
    },
  } as const;
  const variant = normalized === 'win' || normalized === 'completed'
    ? 'success'
    : normalized === 'loss' || normalized === 'invalid_plan'
      ? 'danger'
      : normalized === 'neutral' || normalized === 'no_entry' || normalized === 'partial' || normalized === 'insufficient_data'
        ? 'warning'
        : 'default';
  return <Badge variant={variant}>{labels[language][normalized as keyof typeof labels.zh] ?? normalized}</Badge>;
}

function pct(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${value.toFixed(2)}%`;
}

function money(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--';
  return value.toFixed(2);
}

const PlanBacktestCard: React.FC<{
  plan: CryptoBacktestHistoryPlan;
  language: 'zh' | 'en';
}> = ({ plan, language }) => {
  const text = TEXT[language];
  const latest = plan.latestResult;
  const trade = latest?.trade ?? {};
  return (
    <div className="home-subpanel p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={plan.horizon === 'intraday' ? 'warning' : 'info'}>
          {planTypeLabel(plan.planType, language)}
        </Badge>
        <Badge variant={plan.direction === 'short' ? 'danger' : plan.direction === 'long' ? 'success' : 'default'}>
          {directionLabel(plan.direction, language)}
        </Badge>
        {statusBadge(plan.backtestStatus, language)}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-secondary-text sm:grid-cols-4">
        <span>{text.entry} {plan.entryPrice ?? '--'}</span>
        <span>{text.stopLoss} {plan.stopLoss ?? '--'}</span>
        <span>{text.takeProfit} {plan.takeProfit ?? '--'}</span>
        <span>{text.riskReward} {plan.riskReward || money(trade.rMultiple)}</span>
      </div>

      {latest ? (
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-muted-text sm:grid-cols-5">
          <span>{text.trigger} {latest.entryTriggered ? text.yes : text.no}</span>
          <span>{text.entryPrice} {latest.entryPrice ?? '--'}</span>
          <span>{text.netReturn} {pct(latest.simulatedReturnPct)}</span>
          <span>{text.netPnl} {money(trade.netPnl)}</span>
          <span>{text.rMultiple} {money(trade.rMultiple)}</span>
        </div>
      ) : null}

      {!plan.backtestable ? (
        <p className={`mt-3 text-xs ${plan.qualityStatus === 'no_trade_plan' ? 'text-muted-text' : 'text-danger'}`}>
          {plan.qualityStatus === 'no_trade_plan'
            ? `${text.noTradePlan}${plan.noTradeReason ? `${language === 'zh' ? '：' : ': '}${plan.noTradeReason}` : ''}`
            : plan.missingFields.length === 1 && plan.missingFields[0] === 'execution_contract'
            ? text.legacyContract
            : `${text.missingFields}: ${plan.missingFields.join(', ') || plan.noTradeReason || plan.qualityStatus}`}
        </p>
      ) : null}
    </div>
  );
};

export const ReportBacktestSummary: React.FC<ReportBacktestSummaryProps> = ({
  recordId,
  stockCode,
  language = 'zh',
}) => {
  const reportLanguage = normalizeReportLanguage(language);
  const text = TEXT[reportLanguage];
  const [item, setItem] = useState<CryptoBacktestHistoryItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const shouldRender = Boolean(recordId) && isBitcoinCode(stockCode);

  const fetchBacktest = useCallback(async () => {
    if (!recordId || !shouldRender) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await backtestApi.getHistoryRecord(recordId);
      setItem(response);
    } catch (err) {
      setError(getParsedApiError(err));
      setItem(null);
    } finally {
      setIsLoading(false);
    }
  }, [recordId, shouldRender]);

  useEffect(() => {
    setItem(null);
    setError(null);
    if (shouldRender) {
      void fetchBacktest();
    }
  }, [fetchBacktest, shouldRender]);

  if (!shouldRender) {
    return null;
  }

  return (
    <Card variant="bordered" padding="md" className="home-panel-card">
      <DashboardPanelHeader
        eyebrow={text.eyebrow}
        title={text.title}
        className="mb-3"
        leading={<BarChart3 className="h-4 w-4 text-primary" />}
        actions={(
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void fetchBacktest()}
              disabled={isLoading}
              className="home-accent-link inline-flex items-center gap-1 text-xs disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
              {text.refresh}
            </button>
            <Link
              to="/backtest"
              className="home-accent-pill-link inline-flex items-center gap-1 px-2.5 py-1 text-xs"
            >
              {text.openBacktest}
              <ExternalLink className="h-3.5 w-3.5" />
            </Link>
          </div>
        )}
      />

      {error && !isLoading ? (
        <ApiErrorAlert
          error={error}
          actionLabel={text.refresh}
          onAction={() => void fetchBacktest()}
        />
      ) : null}

      {isLoading && !error ? (
        <DashboardStateBlock compact loading title={text.loading} />
      ) : null}

      {!isLoading && !error && item && item.plans.length === 0 ? (
        <DashboardStateBlock compact title={text.emptyTitle} description={text.emptyDescription} />
      ) : null}

      {!isLoading && !error && item && item.plans.length > 0 ? (
        <div className="grid gap-3 xl:grid-cols-3">
          {item.plans.map((plan) => (
            <PlanBacktestCard
              key={`${item.analysisHistoryId}:${plan.planType}`}
              plan={plan}
              language={reportLanguage}
            />
          ))}
        </div>
      ) : null}
    </Card>
  );
};
