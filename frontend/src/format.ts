/** Display helpers. Anything unknown renders as an em dash, never as 0. */

export const DASH = '—';

export function num(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : v.toFixed(digits);
}

export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (v >= 1) return v.toFixed(4);
  if (v >= 0.0001) return v.toFixed(6);
  return v.toExponential(4);
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

export function compact(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function clock(ms: number | null | undefined): string {
  if (!ms) return DASH;
  return new Date(ms).toLocaleTimeString('en-GB', { hour12: false });
}

export function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'muted';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
}

/** Score colour bands. Deliberately coarse: a score is a rank, not a measurement. */
export function scoreColor(score: number): string {
  if (score >= 72) return 'var(--up)';
  if (score >= 55) return 'var(--warn)';
  if (score >= 35) return 'var(--text)';
  return 'var(--text-faint)';
}

export function maturityColor(score: number): string {
  if (score >= 70) return 'var(--down)';
  if (score >= 50) return 'var(--warn)';
  return 'var(--up)';
}
