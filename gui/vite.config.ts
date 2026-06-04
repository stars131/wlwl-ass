import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

const DEFAULT_WEB_PORT = 1420;
const WEB_PORT = Number(process.env['VITE_WEB_PORT'] || DEFAULT_WEB_PORT);
const IS_TAURI_DEV = process.env['TAURI_DEV'] === 'true';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },

  // Browser dev server / HMR settings
  clearScreen: false,
  server: {
    port: WEB_PORT,
    strictPort: IS_TAURI_DEV,
    host: '127.0.0.1',
    watch: {
      // Ignore the optional desktop shell build output.
      ignored: ['**/src-tauri/**'],
    },
  },

  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    target: 'es2022',
    sourcemap: true,
    minify: 'esbuild',
  },

  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
    },
  },
});
