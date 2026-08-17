import type {
  AlertItem, AssetDetail, DeepScanResponse, Health, HorizonResponse, HuntResponse,
  DecisionsResponse, PerformanceResponse, Position, PositionEvent, PumpResponse,
  ResultsResponse, ScanResponse, StartupResponse, TradeSignal, Validation,
  ValidationsResponse, ProvidersResponse, RobinhoodStatus, RobinhoodTokensResponse, TokenDetail,
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

/** POST with a JSON body, surfacing the API's own message on failure.
 *  A refused action must say why: silence would look like a bug. */
async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail ?? `${path} failed: ${resp.status}`);
  }
  return resp.json();
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
  pumps: (symbol: string, bars = 1000) => get<PumpResponse>(`/pumps/${symbol}`, { bars }),
  validations: (limit = 100) => get<ValidationsResponse>('/validations', { limit }),

  decisions: () => get<DecisionsResponse>('/decisions'),
  startup: () => get<StartupResponse>('/startup'),
  results: () => get<ResultsResponse>('/results'),
  positions: (status = 'OPEN') =>
    get<{ positions: Position[]; watcher: Record<string, unknown> }>('/positions', { status }),
  positionEvents: (id: number) =>
    get<{ position: Position; events: PositionEvent[] }>(`/positions/${id}/events`),
  tradeSignals: (limit = 100) => get<{ signals: TradeSignal[] }>('/trade-signals', { limit }),

  openPosition: (body: Record<string, unknown>) => post<Position>('/positions/open', body),
  closePosition: (id: number, body: Record<string, unknown>) =>
    post<Position>(`/positions/${id}/close`, body),
  answerSignal: (id: number, taken: boolean) =>
    post<TradeSignal>(`/trade-signals/${id}/answer`, { taken }),
  watchPositions: () => post<Record<string, unknown>>('/positions/watch', {}),
  currentValidations: () =>
    get<{ current: Record<string, Validation> }>('/validations/current'),
  validate: async (body: Record<string, unknown>): Promise<Validation> => {
    const resp = await fetch(`${BASE}/validations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail ?? `validation failed: ${resp.status}`);
    }
    return resp.json();
  },
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
  providers: () => get<ProvidersResponse>('/providers'),
  robinhoodStatus: () => get<RobinhoodStatus>('/provider/robinhood/status'),
  robinhoodToken: (address: string) => get<TokenDetail>(`/robinhood/token/${address}`),
  robinhoodTokens: (bucket?: string, sort?: string) =>
    get<RobinhoodTokensResponse>('/robinhood/tokens', {
      ...(bucket ? { bucket } : {}), ...(sort ? { sort } : {}),
    }),
  robinhoodSearch: async (): Promise<RobinhoodTokensResponse> => {
    const resp = await fetch(`${BASE}/robinhood/tokens/search`, { method: 'POST' });
    if (!resp.ok) throw new Error(`robinhood search failed: ${resp.status}`);
    return resp.json();
  },
  verifyRobinhood: async (): Promise<RobinhoodStatus> => {
    const resp = await fetch(`${BASE}/provider/robinhood/verify`, { method: 'POST' });
    if (!resp.ok) throw new Error(`robinhood verify failed: ${resp.status}`);
    return resp.json();
  },
  runScan: async () => {
    const resp = await fetch(`${BASE}/scan/run`, { method: 'POST' });
    if (!resp.ok) throw new Error(`scan failed: ${resp.status}`);
    return resp.json();
  },
};
