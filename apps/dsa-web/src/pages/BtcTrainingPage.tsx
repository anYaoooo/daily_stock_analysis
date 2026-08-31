import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Beaker, BrainCircuit, Database, Download, Play, RefreshCw, ShieldAlert } from 'lucide-react';
import { btcTrainingApi } from '../api/btcTraining';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Card, PageHeader } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type {
  BtcTrainingArchitecture,
  BtcTrainingConfig,
  BtcTrainingResearchResult,
  BtcTrainingTaskStatus,
} from '../types/btcTraining';
import type { UiTextKey } from '../i18n/uiText';
import { cn } from '../utils/cn';

const textKeys = {
  eyebrow: 'training.eyebrow',
  title: 'training.title',
  description: 'training.description',
  refresh: 'training.refresh',
  architecture: 'training.architecture',
  allArchitectures: 'training.allArchitectures',
  epochs: 'training.epochs',
  seeds: 'training.seeds',
  seedsHint: 'training.seedsHint',
  sequenceLength: 'training.sequenceLength',
  ablation: 'training.ablation',
  ablationHint: 'training.ablationHint',
  run: 'training.run',
  running: 'training.running',
  protocol: 'training.protocol',
  featureCount: 'training.featureCount',
  seedCount: 'training.seedCount',
  validationWindow: 'training.validationWindow',
  fixed: 'training.fixed',
  researchOnly: 'training.researchOnly',
  noPromotion: 'training.noPromotion',
  results: 'training.results',
  noResults: 'training.noResults',
  oofSamples: 'training.oofSamples',
  available: 'training.available',
  errorTitle: 'training.errorTitle',
  configError: 'training.configError',
  downloadSummary: 'training.downloadSummary',
  downloadOof: 'training.downloadOof',
} as const satisfies Record<string, UiTextKey>;

const inputClass = 'h-10 rounded-xl border border-border/70 bg-surface-2/60 px-3 text-sm text-foreground outline-none transition focus:border-cyan/60 focus:ring-2 focus:ring-cyan/20';

function summarizeOof(result: BtcTrainingResearchResult | undefined): number {
  return result?.oofPredictionCount ?? 0;
}

function runMetric(run: BtcTrainingResearchResult['runs'][number], key: 'directionAccuracy' | 'returnMae'): string {
  const values = Object.values(run.evaluations ?? {})
    .map((evaluation) => evaluation[key])
    .filter((value): value is number => typeof value === 'number');
  if (!values.length) return '-';
  return (values.reduce((total, value) => total + value, 0) / values.length).toFixed(4);
}

const BtcTrainingPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [config, setConfig] = useState<BtcTrainingConfig | null>(null);
  const [architecture, setArchitecture] = useState<BtcTrainingArchitecture>('patchtst');
  const [epochs, setEpochs] = useState(20);
  const [seeds, setSeeds] = useState('7,13,29,43,71');
  const [sequenceLength, setSequenceLength] = useState(256);
  const [ablationFeatures, setAblationFeatures] = useState<string[]>([]);
  const [task, setTask] = useState<BtcTrainingTaskStatus | null>(null);
  const [result, setResult] = useState<BtcTrainingResearchResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const pollTimer = useRef<number | null>(null);
  const pollGeneration = useRef(0);

  useEffect(() => () => {
    pollGeneration.current += 1;
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
  }, []);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const next = await btcTrainingApi.getConfig();
      setConfig(next);
      setEpochs((value) => Math.max(value, next.minEpochs));
      setError(null);
    } catch (err) {
      setError({ ...getParsedApiError(err), title: t(textKeys.configError) });
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  const seedValues = useMemo(() => {
    const values = seeds.split(',').map((value) => Number(value.trim())).filter((value) => Number.isInteger(value));
    return Array.from(new Set(values));
  }, [seeds]);

  const pollTask = useCallback(async (taskId: string, generation: number) => {
    try {
      const next = await btcTrainingApi.getTask(taskId);
      if (generation !== pollGeneration.current) return;
      setTask(next);
      if (next.status === 'pending' || next.status === 'processing') {
        pollTimer.current = window.setTimeout(() => void pollTask(taskId, generation), 1500);
        return;
      }
      setRunning(false);
      if (next.status === 'completed' && next.result) {
        setResult(next.result);
        setError(null);
      } else if (next.error) {
        setError({ ...getParsedApiError(new Error(next.error)), title: t(textKeys.errorTitle) });
      }
    } catch (err) {
      if (generation !== pollGeneration.current) return;
      setRunning(false);
      setError({ ...getParsedApiError(err), title: t(textKeys.errorTitle) });
    }
  }, [t]);

  const startTraining = async () => {
    if (!config || seedValues.length < config.minSeeds || epochs < config.minEpochs) return;
    setRunning(true);
    setResult(null);
    setError(null);
    try {
      const accepted = await btcTrainingApi.run({
        architecture,
        epochs,
        seeds: seedValues,
        sequenceLength,
        ablationFeatures,
      });
      setTask({ taskId: accepted.taskId, status: accepted.status, progress: 0, message: accepted.message });
      const generation = pollGeneration.current + 1;
      pollGeneration.current = generation;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
      void pollTask(accepted.taskId, generation);
    } catch (err) {
      setRunning(false);
      setError({ ...getParsedApiError(err), title: t(textKeys.errorTitle) });
    }
  };

  const downloadArtifact = async (kind: 'summary' | 'oof') => {
    if (!task?.taskId || !result) return;
    try {
      await btcTrainingApi.downloadArtifact(task.taskId, kind);
    } catch (err) {
      setError({ ...getParsedApiError(err), title: t(textKeys.errorTitle) });
    }
  };

  const toggleAblationFeature = (feature: string) => {
    setAblationFeatures((current) => current.includes(feature) ? current.filter((item) => item !== feature) : [...current, feature]);
  };

  return (
    <AppPage>
      <div className="space-y-5">
        <PageHeader
          eyebrow={t(textKeys.eyebrow)}
          title={t(textKeys.title)}
          description={t(textKeys.description)}
          actions={(
            <button type="button" className="btn-secondary inline-flex items-center gap-2" onClick={() => void loadConfig()} disabled={loading || running}>
              <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
              {t(textKeys.refresh)}
            </button>
          )}
        />

        {error ? <ApiErrorAlert error={error} actionLabel={t('common.retry')} onAction={() => void loadConfig()} /> : null}

        <div className="grid gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <Card title={t(textKeys.title)} subtitle={t(textKeys.eyebrow)}>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1.5 text-sm text-secondary-text">
                <span>{t(textKeys.architecture)}</span>
                <select className={`${inputClass} w-full`} value={architecture} onChange={(event) => setArchitecture(event.target.value as BtcTrainingArchitecture)}>
                  <option value="patchtst">PatchTST</option>
                  <option value="itransformer">iTransformer</option>
                  <option value="fusion">Fusion</option>
                  <option value="all">{t(textKeys.allArchitectures)}</option>
                </select>
              </label>
              <label className="space-y-1.5 text-sm text-secondary-text">
                <span>{t(textKeys.epochs)}</span>
                <input className={`${inputClass} w-full`} type="number" min={config?.minEpochs ?? 20} value={epochs} onChange={(event) => setEpochs(Number(event.target.value))} />
              </label>
              <label className="space-y-1.5 text-sm text-secondary-text sm:col-span-2">
                <span>{t(textKeys.seeds)}</span>
                <input className={`${inputClass} w-full`} value={seeds} onChange={(event) => setSeeds(event.target.value)} />
                <span className="block text-xs text-secondary-text">{t(textKeys.seedsHint, { count: config?.minSeeds ?? 5 })}</span>
              </label>
              <label className="space-y-1.5 text-sm text-secondary-text">
                <span>{t(textKeys.sequenceLength)}</span>
                <input className={`${inputClass} w-full`} type="number" min={8} value={sequenceLength} onChange={(event) => setSequenceLength(Number(event.target.value))} />
              </label>
            </div>

            <div className="mt-5 border-t border-border/60 pt-4">
              <div className="flex items-center gap-2 text-sm font-medium text-foreground"><Beaker className="h-4 w-4 text-cyan" />{t(textKeys.ablation)}</div>
              <p className="mt-1 text-xs text-secondary-text">{t(textKeys.ablationHint)}</p>
              <div className="mt-3 grid max-h-64 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
                {(config?.featureColumns ?? []).map((feature) => (
                  <label key={feature} className="flex items-start gap-2 rounded-lg border border-border/50 bg-surface-2/40 px-2.5 py-2 text-xs text-secondary-text">
                    <input type="checkbox" checked={ablationFeatures.includes(feature)} onChange={() => toggleAblationFeature(feature)} className="mt-0.5 accent-cyan" />
                    <span className="break-all">{feature}</span>
                  </label>
                ))}
              </div>
            </div>

            <button type="button" className="btn-primary mt-5 inline-flex w-full items-center justify-center gap-2" onClick={() => void startTraining()} disabled={loading || running || !config || seedValues.length < (config?.minSeeds ?? 5) || epochs < (config?.minEpochs ?? 20)}>
              {running ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {running ? t(textKeys.running) : t(textKeys.run)}
            </button>
          </Card>

          <Card title={t(textKeys.protocol)} subtitle={t(textKeys.researchOnly)}>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-border/60 bg-surface-2/40 p-3"><Database className="h-4 w-4 text-cyan" /><p className="mt-2 text-xs text-secondary-text">{t(textKeys.featureCount)}</p><p className="text-lg font-semibold text-foreground">{config?.featureCount ?? '-'}</p></div>
              <div className="rounded-xl border border-border/60 bg-surface-2/40 p-3"><BrainCircuit className="h-4 w-4 text-cyan" /><p className="mt-2 text-xs text-secondary-text">{t(textKeys.seedCount)}</p><p className="text-lg font-semibold text-foreground">{seedValues.length}</p></div>
              <div className="rounded-xl border border-border/60 bg-surface-2/40 p-3 sm:col-span-2"><p className="text-xs text-secondary-text">{t(textKeys.validationWindow)}</p><p className="mt-1 font-medium text-foreground">{t(textKeys.fixed)} · {t(textKeys.noPromotion)}</p></div>
            </div>
            {task && (task.status === 'pending' || task.status === 'processing') ? <div className="mt-5 rounded-xl border border-cyan/30 bg-cyan/5 p-3 text-sm text-cyan">{task.message || t(textKeys.running)} · {task.progress}%</div> : null}
            {result ? <div className="mt-5 rounded-xl border border-amber-400/30 bg-amber-400/5 p-3 text-sm text-amber-200"><ShieldAlert className="mr-2 inline h-4 w-4" />{t(textKeys.noPromotion)}<div className="mt-3 flex flex-wrap gap-2"><button type="button" className="btn-secondary inline-flex items-center gap-2" onClick={() => void downloadArtifact('summary')}><Download className="h-4 w-4" />{t(textKeys.downloadSummary)}</button><button type="button" className="btn-secondary inline-flex items-center gap-2" onClick={() => void downloadArtifact('oof')}><Download className="h-4 w-4" />{t(textKeys.downloadOof)}</button></div></div> : null}
          </Card>
        </div>

        <Card title={t(textKeys.results)} subtitle={result ? `${result.protocol.seedCount} seeds · ${result.protocol.epochs} epochs` : undefined}>
          {!result ? <p className="py-8 text-center text-sm text-secondary-text">{t(textKeys.noResults)}</p> : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-border/60 text-sm">
                <thead className="text-left text-xs text-secondary-text"><tr><th className="px-3 py-2">架构</th><th className="px-3 py-2">seed</th><th className="px-3 py-2">特征</th><th className="px-3 py-2">OOF</th><th className="px-3 py-2">方向准确率</th><th className="px-3 py-2">收益 MAE</th><th className="px-3 py-2">状态</th></tr></thead>
                <tbody className="divide-y divide-border/50">{result.runs.map((run, index) => <tr key={`${run.architecture}-${run.seed}-${run.removedFeature ?? 'full'}-${index}`}><td className="px-3 py-2 font-medium text-foreground">{run.architecture}</td><td className="px-3 py-2 text-secondary-text">{run.seed}</td><td className="px-3 py-2 text-secondary-text">{run.featureCount}{run.removedFeature ? ` (-${run.removedFeature})` : ''}</td><td className="px-3 py-2 text-secondary-text">{run.oofPredictionCount}</td><td className="px-3 py-2 text-secondary-text">{runMetric(run, 'directionAccuracy')}</td><td className="px-3 py-2 text-secondary-text">{runMetric(run, 'returnMae')}</td><td className="px-3 py-2 text-secondary-text">{run.dataQuality || t(textKeys.available)}</td></tr>)}</tbody>
              </table>
              <p className="mt-3 text-xs text-secondary-text">{t(textKeys.oofSamples)}: {summarizeOof(result)}</p>
            </div>
          )}
        </Card>
      </div>
    </AppPage>
  );
};

export default BtcTrainingPage;
