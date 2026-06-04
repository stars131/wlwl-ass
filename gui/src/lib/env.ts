/**
 * Resolve the Python backend base URL.
 *
 * The web launcher starts `launcher.api_server` on a local port and passes
 * that URL through `VITE_GA_API_BASE`. In standalone Vite dev, fall back to
 * the default API port the developer can run manually.
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
