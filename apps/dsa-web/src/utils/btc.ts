const BTC_ANALYSIS_ALIASES = new Set([
  'BTC',
  'BTCUSDT',
  'BTC-USD',
  'BTC/USD',
  'BTCUSD',
]);

export function normalizeBtcAnalysisCode(value: string): 'BTC' | null {
  const normalized = value.trim().toUpperCase();
  return BTC_ANALYSIS_ALIASES.has(normalized) ? 'BTC' : null;
}

