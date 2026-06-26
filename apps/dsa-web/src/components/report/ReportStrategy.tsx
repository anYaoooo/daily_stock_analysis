import type React from 'react';
import type {
  DirectionalStrategyPlan,
  IntradayStrategyPlan,
  ReportLanguage,
  ReportStrategy as ReportStrategyType,
} from '../../types/analysis';
import { Card } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportStrategyProps {
  strategy?: ReportStrategyType;
  language?: ReportLanguage;
}

interface StrategyItemProps {
  label: string;
  value?: string;
  tone: string;
}

interface DirectionalPlanCardProps {
  title: string;
  plan: DirectionalStrategyPlan | IntradayStrategyPlan;
  tone: string;
  text: ReturnType<typeof getReportText>;
  includeDailyConstraint?: boolean;
}

const StrategyItem: React.FC<StrategyItemProps> = ({
  label,
  value,
  tone,
}) => (
  <div className="home-subpanel home-strategy-card p-3" style={{ ['--home-strategy-tone' as string]: `var(${tone})` }}>
    <div className="flex flex-col">
      <span className="home-strategy-label mb-0.5 text-xs">{label}</span>
      <span className="home-strategy-value text-lg font-bold font-mono" style={!value ? { color: 'var(--text-muted-text)' } : undefined}>
        {value || '—'}
      </span>
    </div>
    <div
      className="absolute bottom-0 left-0 right-0 h-0.5"
      style={{ background: `linear-gradient(90deg, transparent, var(${tone}), transparent)` }}
    />
  </div>
);

const hasPlanValue = (plan?: DirectionalStrategyPlan | null): plan is DirectionalStrategyPlan =>
  Boolean(plan && Object.values(plan).some((value) => typeof value === 'string' && value.trim()));

const hasIntradayPlanValue = (plan?: IntradayStrategyPlan | null): plan is IntradayStrategyPlan =>
  Boolean(plan && Object.values(plan).some((value) => {
    if (typeof value === 'boolean') {
      return true;
    }
    return typeof value === 'string' && value.trim();
  }));

const DirectionalPlanCard: React.FC<DirectionalPlanCardProps> = ({
  title,
  plan,
  tone,
  text,
  includeDailyConstraint = false,
}) => {
  const rows = [
    { label: text.analysisMode, value: plan.analysisTimeframe },
    { label: text.direction, value: 'direction' in plan ? plan.direction : undefined },
    { label: text.entryZone, value: plan.entryZone },
    { label: text.entryPrice, value: plan.entryPrice },
    { label: text.stopLoss, value: plan.stopLoss },
    { label: text.takeProfit, value: plan.takeProfit },
    { label: text.triggerCondition, value: plan.triggerCondition },
    { label: text.invalidation, value: plan.invalidation || plan.invalidCondition },
    { label: text.riskReward, value: plan.riskReward },
    { label: text.positionHint, value: plan.positionHint },
    { label: text.confidence, value: plan.confidence },
    { label: text.noTradeReason, value: plan.noTradeReason },
    { label: text.dailyConstraint, value: includeDailyConstraint && 'dailyConstraint' in plan ? plan.dailyConstraint : undefined },
    { label: text.reason, value: plan.reason },
  ].filter((row) => row.value && row.value.trim());

  return (
    <div className="home-subpanel home-strategy-card p-3" style={{ ['--home-strategy-tone' as string]: `var(${tone})` }}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="home-strategy-label text-xs">{title}</span>
        {plan.analysisTimeframe ? <span className="text-[11px] text-muted-text">{plan.analysisTimeframe}</span> : null}
      </div>
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.label} className="grid grid-cols-[72px_1fr] gap-2 text-sm">
            <span className="text-muted-text">{row.label}</span>
            <span className="font-medium text-primary-text">{row.value}</span>
          </div>
        ))}
      </div>
      <div
        className="absolute bottom-0 left-0 right-0 h-0.5"
        style={{ background: `linear-gradient(90deg, transparent, var(${tone}), transparent)` }}
      />
    </div>
  );
};

/**
 * 策略点位区组件 - 终端风格
 */
export const ReportStrategy: React.FC<ReportStrategyProps> = ({ strategy, language = 'zh' }) => {
  if (!strategy) {
    return null;
  }

  const reportLanguage = normalizeReportLanguage(language);
  const text = getReportText(reportLanguage);
  const hasDirectionalPlans = hasPlanValue(strategy.longPlan) || hasPlanValue(strategy.shortPlan);
  const intradayPlan = hasIntradayPlanValue(strategy.intradayPlan) ? strategy.intradayPlan : null;

  const strategyItems = [
    {
      label: text.idealBuy,
      value: strategy.idealBuy,
      tone: '--home-strategy-buy',
    },
    {
      label: text.secondaryBuy,
      value: strategy.secondaryBuy,
      tone: '--home-strategy-secondary',
    },
    {
      label: text.stopLoss,
      value: strategy.stopLoss,
      tone: '--home-strategy-stop',
    },
    {
      label: text.takeProfit,
      value: strategy.takeProfit,
      tone: '--home-strategy-take',
    },
  ];

  return (
    <Card variant="bordered" padding="md" className="home-panel-card">
      <DashboardPanelHeader
        eyebrow={text.strategyPoints}
        title={hasDirectionalPlans ? text.directionalPlans : text.sniperLevels}
        className="mb-3"
      />
      {hasDirectionalPlans ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {hasPlanValue(strategy.longPlan) && (
              <DirectionalPlanCard
                title={text.longPlan}
                plan={strategy.longPlan}
                tone="--home-strategy-buy"
                text={text}
              />
            )}
            {hasPlanValue(strategy.shortPlan) && (
              <DirectionalPlanCard
                title={text.shortPlan}
                plan={strategy.shortPlan}
                tone="--home-strategy-stop"
                text={text}
              />
            )}
          </div>
          {intradayPlan && (
            <DirectionalPlanCard
              title={text.intradayPlan}
              plan={intradayPlan}
              tone="--home-strategy-secondary"
              text={text}
              includeDailyConstraint
            />
          )}
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {strategyItems.map((item) => (
            <StrategyItem key={item.label} {...item} />
          ))}
        </div>
      )}
      {!hasDirectionalPlans && intradayPlan && (
        <div className="mt-3">
          <DirectionalPlanCard
            title={text.intradayPlan}
            plan={intradayPlan}
            tone="--home-strategy-secondary"
            text={text}
            includeDailyConstraint
          />
        </div>
      )}
    </Card>
  );
};
