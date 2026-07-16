import { configDefaults, defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export const legacyStockTestFiles = [
  'src/api/__tests__/decisionSignals.test.ts',
  'src/components/decision-signals/__tests__/DecisionSignalDisplay.test.tsx',
  'src/components/report/__tests__/MarketReviewReportView.test.tsx',
  'src/components/report/__tests__/ReportDecisionSignals.test.tsx',
  'src/components/settings/__tests__/IntelligentImport.test.tsx',
  'src/pages/__tests__/DecisionSignalsPage.test.tsx',
  'src/pages/__tests__/HomePage.test.tsx',
];

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.ts',
    exclude: [
      ...configDefaults.exclude,
      'e2e/**',
      'playwright.config.ts',
      ...legacyStockTestFiles,
    ],
  },
});
