import type {
  AlertItem, AssetDetail, DeepScanResponse, Health, HorizonResponse, HuntResponse,
  PerformanceResponse, ScanResponse,
} from './types';

const BASE = '/api';

async function get<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== undefined) url.searchParams.set(k, String(v));
    }
  }
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    // 503 with NO_SCAN_YET is an expected state, not an exception path.
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body.reason) detail = body.reason;
      else if (body.detail) detail = body.detail;
    } catch { /* body was not JSON */ }
    throw new Error(detail);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => get<Health>('/health'),
  scan: (params?: Record<string, string | number | boolean>) => get<ScanResponse>('/scan', params),
  top: (limit = 6) => get<{ results: ScanResponse['results'] }>('/scan/top', { limit }),
  asset: (symbol: string) => get<AssetDetail>(`/asset/${symbol}`),
  alerts: (limit = 50) => get<{ alerts: AlertItem[] }>('/alerts', { limit }),
  signals: (limit = 100) =>
    get<{ stats: Record<string, unknown>; signals: Record<string, unknown>[] }>('/signals', { limit }),
  performance: () => get<PerformanceResponse>('/performance'),
  horizons: () => get<HorizonResponse>('/horizons'),
  hunt: (limit = 40) => get<HuntResponse>('/hunt', { limit }),
  deepScan: async (maxSymbols = 40): Promise<DeepScanResponse> => {
    const resp = await fetch(`${BASE}/hunt/deep?max_symbols=${maxSymbols}`, { method: 'POST' });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.reason ?? `deep scan failed: ${resp.status}`);
    }
    return resp.json();
  },
  trackHorizons: async () => {
    const resp = await fetch(`${BASE}/horizons/track`, { method: 'POST' });
    if (!resp.ok) throw new Error(`horizon tracking failed: ${resp.status}`);
    return resp.json();
  },
  resolveOutcomes: async () => {
    const resp = await fetch(`${BASE}/outcomes/resolve`, { method: 'POST' });
    if (!resp.ok) throw new Error(`resolve failed: ${resp.status}`);
    return resp.json();
  },
  runScan: async () => {
    const resp = await fetch(`${BASE}/scan/run`, { method: 'POST' });
    if (!resp.ok) throw new Error(`scan failed: ${resp.status}`);
    return resp.json();
  },
};
