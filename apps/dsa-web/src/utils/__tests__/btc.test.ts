import { describe, expect, it } from 'vitest';

import { normalizeBtcAnalysisCode } from '../btc';

describe('normalizeBtcAnalysisCode', () => {
  it.each(['BTC', 'btc', 'BTCUSDT', 'BTC-USD', 'BTC/USD', 'BTCUSD'])(
    'normalizes %s to BTC',
    (value) => {
      expect(normalizeBtcAnalysisCode(value)).toBe('BTC');
    },
  );

  it.each(['AAPL', '600519', 'ETH', 'Bitcoin', ''])('rejects non-BTC analysis input %s', (value) => {
    expect(normalizeBtcAnalysisCode(value)).toBeNull();
  });
});

