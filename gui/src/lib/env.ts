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
    __GA_API_AUTH_TOKEN__?: string;
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

export function getApiAuthToken(): string {
  if (typeof window !== 'undefined' && window.__GA_API_AUTH_TOKEN__) {
    return window.__GA_API_AUTH_TOKEN__;
  }
  const fromEnv = import.meta.env['VITE_GA_API_AUTH_TOKEN'];
  return typeof fromEnv === 'string' ? fromEnv : '';
}

export function apiHeaders(extra: HeadersInit = {}): HeadersInit {
  const headers = new Headers(extra);
  if (!headers.has('content-type')) headers.set('content-type', 'application/json');
  const token = getApiAuthToken().trim();
  if (token && !headers.has('authorization')) {
    headers.set('authorization', `Bearer ${token}`);
  }
  return headers;
}

export function appendAuthTokenParam(url: string): string {
  const token = getApiAuthToken().trim();
  if (!token) return url;
  const u = new URL(url, typeof window !== 'undefined' ? window.location.href : undefined);
  u.searchParams.set('access_token', token);
  return u.toString();
}
