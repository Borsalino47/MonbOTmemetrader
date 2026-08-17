/** Display helpers. Anything unknown renders as an em dash, never as 0.
 *
 * FRENCH TYPOGRAPHY, APPLIED HERE AND NOWHERE ELSE
 *
 * Decimal comma, narrow no-break space (U+202F) before a unit and as the
 * thousands separator. Doing it in one module is what stops "2.59" and "2,59"
 * from appearing on the same screen — and the narrow *no-break* space matters
 * on a phone, where a plain space lets "%" wrap onto the next line.
 *
 * These are display functions only. No value is rounded before it reaches the
 * screen, and nothing here is read back into a calculation (spec §13).
 */

export const DASH = '—';

/** Narrow no-break space, the separator French typography requires. */
const NBSP = ' ';

/** Decimal comma, and a narrow space every three digits of the integer part.
 *
 * The split on the decimal point is the point: grouping the whole string would
 * also insert separators inside the decimals, turning 0,123456 into 0,123 456.
 */
function group(v: number, digits: number): string {
  const [int, dec] = v.toFixed(digits).split('.');
  const sign = int.startsWith('-') ? '-' : '';
  const grouped = (sign ? int.slice(1) : int).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  return dec ? `${sign}${grouped},${dec}` : `${sign}${grouped}`;
}

export function num(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : group(v, digits);
}

export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (v >= 1000) return group(v, 2);
  if (v >= 1) return group(v, 4);
  if (v >= 0.0001) return group(v, 6);
  return v.toExponential(4).replace('.', ',');
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${v >= 0 ? '+' : ''}${group(v, digits)}${NBSP}%`;
}

/** A multiple: `1.3` -> `1,3×`. The multiplication sign, not the letter x. */
export function mult(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${group(v, digits)}×`;
}

export function compact(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  // Md = milliard. The French abbreviations, so a volume does not read as
  // English on a French screen.
  if (v >= 1e9) return `${group(v / 1e9, 2)}${NBSP}Md`;
  if (v >= 1e6) return `${group(v / 1e6, 1)}${NBSP}M`;
  if (v >= 1e3) return `${group(v / 1e3, 0)}${NBSP}k`;
  return group(v, 0);
}

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 60) return `${Math.round(seconds)}${NBSP}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}${NBSP}min ${Math.round(seconds % 60)}${NBSP}s`;
  return `${Math.floor(seconds / 3600)}${NBSP}h ${Math.floor((seconds % 3600) / 60)}${NBSP}min`;
}

export function clock(ms: number | null | undefined): string {
  if (!ms) return DASH;
  return new Date(ms).toLocaleTimeString('fr-FR', { hour12: false });
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
