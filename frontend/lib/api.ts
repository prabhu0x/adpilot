export const API_ROOT = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1').replace(/\/$/, '');
export const API_ORIGIN = API_ROOT.replace(/\/api\/v1$/, '');

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  constructor(message: string, status: number, code = 'request_failed', details?: unknown) {
    super(message); this.name = 'ApiError'; this.status = status; this.code = code; this.details = details;
  }
}

export function accessToken() {
  if (typeof window === 'undefined') return '';
  return localStorage.getItem('adpilot.access') || '';
}
export function saveTokens(access: string, refresh?: string) {
  localStorage.setItem('adpilot.access', access);
  if (refresh) localStorage.setItem('adpilot.refresh', refresh);
}
export function clearTokens() {
  localStorage.removeItem('adpilot.access'); localStorage.removeItem('adpilot.refresh');
}

let refreshInFlight: Promise<string> | null = null;
async function refreshAccessToken(): Promise<string> {
  const refresh = typeof window !== 'undefined' ? localStorage.getItem('adpilot.refresh') : null;
  if (!refresh) return '';
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${API_ROOT}/auth/refresh`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }), cache: 'no-store',
    }).then(async response => {
      if (!response.ok) throw new Error('Refresh token expired');
      const body = await response.json();
      saveTokens(body.access_token);
      return body.access_token as string;
    }).catch(() => { clearTokens(); return ''; }).finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const url = `${API_ROOT}${path.startsWith('/') ? path : `/${path}`}`;
  const headers = new Headers(init.headers || {});
  if (!(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const token = accessToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const request = (requestHeaders: Headers) => fetch(url, { ...init, headers: requestHeaders, cache: 'no-store' });
  let response = await request(headers);
  if (response.status === 401 && !path.includes('/auth/login') && !path.includes('/auth/refresh')) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      const retryHeaders = new Headers(headers);
      retryHeaders.set('Authorization', `Bearer ${refreshed}`);
      response = await request(retryHeaders);
    }
  }
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const e = body?.error || {};
    throw new ApiError(e.message || `Request failed (${response.status})`, response.status, e.code, e.details);
  }
  return body as T;
}
export const get = <T = any>(path: string) => api<T>(path);
export const post = <T = any>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
export const patch = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
export const put = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(body) });

export function csvDownload(name: string, rows: Record<string, unknown>[]) {
  if (!rows.length) return;
  const keys = Object.keys(rows[0]);
  const quote = (v: unknown) => `"${String(v ?? '').replaceAll('"', '""')}"`;
  const text = [keys.map(quote).join(','), ...rows.map(r => keys.map(k => quote(r[k])).join(','))].join('\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
}
