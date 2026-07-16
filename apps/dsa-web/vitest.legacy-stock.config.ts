import react from '@vitejs/plugin-react';
import { configDefaults, defineConfig } from 'vitest/config';
import { legacyStockTestFiles } from './vitest.config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.ts',
    include: legacyStockTestFiles,
    exclude: [...configDefaults.exclude, 'e2e/**', 'playwright.config.ts'],
  },
});
