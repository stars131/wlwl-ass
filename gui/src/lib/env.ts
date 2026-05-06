/**
 * Resolve the Python backend base URL.
 *
 * In production (Tauri shell), the Rust side spawns `launcher.api_server` on
 * a free port and injects the URL into `window.__GA_API_BASE__` before the
 * React app boots. In `vite dev` (browser-only), we fall back to a known port
 * the developer can run manually with `python -m launcher.api_server`.
 */

declare global {
  interface Window {
    __GA_API_BASE__?: string;
  }
}

const DEV_FALLBACK = 'http://127.0.0.1:18800';

export function getApiBase(): string {
  if (typeof window !== 'undefined' && window.__GA_API_BASE__) {
    return window.__GA_API_BASE__.replace(/\/+$/, '');
  }
  const fromEnv = import.meta.env['VITE_GA_API_BASE'];
  if (typeof fromEnv === 'string' && fromEnv.length > 0) {
    return fromEnv.replace(/\/+$/, '');
  }
  return DEV_FALLBACK;
}
