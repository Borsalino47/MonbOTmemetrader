/** French labels for the interface.
 *
 * Only chrome lives here — headings, column names, buttons. Everything the
 * engines *say* (reasons, risks, triggers, invalidations) is already French
 * when it arrives: it is assembled in Python where the numbers are, from
 * `cryptopulse/i18n/reasons.py`. Translating it a second time here would mean
 * parsing prose to recover figures, and every new reason would ship in English
 * until somebody noticed.
 *
 * Enum values stay English on the wire — they are identifiers stored in SQLite
 * — and the API sends a `*_label_fr` alongside. The tables below are the
 * fallback for payloads that predate those labels; a component must prefer the
 * server's label when it has one. */

export const SETUP_STATE_FR: Record<string, string> = {
  IGNORE: 'IGNORER',
  OBSERVE: 'OBSERVER',
  WATCH: 'SURVEILLER',
  ARMED: 'PRÊT',
  BREAKOUT: 'CASSURE',
  RETEST: 'RETEST',
  CONTINUATION: 'CONTINUATION',
  INVALIDATED: 'INVALIDÉ',
};

export const SETUP_STATE_LONG_FR: Record<string, string> = {
  IGNORE: 'IGNORER',
  OBSERVE: 'OBSERVER',
  WATCH: 'SURVEILLER',
  ARMED: 'PRÊT — EN ATTENTE DU DÉCLENCHEMENT',
  BREAKOUT: 'CASSURE CONFIRMÉE',
  RETEST: 'RETEST DU NIVEAU CASSÉ',
  CONTINUATION: 'CONTINUATION DE LA HAUSSE',
  INVALIDATED: 'FAUSSE CASSURE — SETUP INVALIDÉ',
};

export const ALERT_LEVEL_FR: Record<string, string> = {
  INFO: 'INFORMATION',
  WATCH: 'SURVEILLER',
  HIGH: 'ÉLEVÉ',
  CRITICAL_SETUP: 'CRITIQUE',
};

export const LIQUIDITY_FR: Record<string, string> = {
  EXCELLENT: 'EXCELLENTE',
  GOOD: 'BONNE',
  ACCEPTABLE: 'ACCEPTABLE',
  POOR: 'FAIBLE',
  DANGEROUS: 'DANGEREUSE',
  UNKNOWN: 'INCONNUE',
};

export const OUTCOME_FR: Record<string, string> = {
  WIN: 'GAGNANT',
  LOSS: 'PERDANT',
  TIMEOUT: 'SANS ISSUE',
  UNRESOLVABLE: 'NON RÉSOLU',
  PENDING: 'EN ATTENTE',
};

/** Look up a label without ever falling back to the English identifier.
 *
 * `str(value)` as a fallback is how "BREAKOUT" ends up on a French screen: it
 * is readable enough that nobody notices it was never translated. An em dash
 * is visibly missing, which is the point. */
export function labelFor(table: Record<string, string>, key: string | null | undefined): string {
  if (!key) return '—';
  return table[key] ?? '—';
}

/** French number formatting: decimal comma, narrow space before the unit. */
export function frNum(v: number | null | undefined, nd = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return v.toLocaleString('fr-FR', { minimumFractionDigits: nd, maximumFractionDigits: nd });
}

export function frPct(v: number | null | undefined, nd = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v > 0 ? '+' : ''}${frNum(v, nd)} %`;
}

/** The eight weighted components, as the score breakdown names them.
 *
 * The keys are the engine's own identifiers and stay English on the wire — the
 * weights fingerprint is computed over them, so renaming one would silently
 * reinterpret every past signal (invariant 13). */
export const COMPONENT_FR: Record<string, string> = {
  volume: 'Volume',
  momentum: 'Momentum',
  structure: 'Structure',
  breakout: 'Cassure',
  volatility: 'Volatilité',
  orderflow: 'Carnet d’ordres',
  mtf: 'Multi-unités de temps',
  liquidity: 'Liquidité',
};

/** Per-timeframe directional bias. */
export const BIAS_FR: Record<string, string> = {
  BULLISH: 'HAUSSIER',
  BEARISH: 'BAISSIER',
  NEUTRAL: 'NEUTRE',
};


/** Severity of a security finding. `UNKNOWN` is deliberately "INCONNU" rather
 *  than anything reassuring: a check that was never run is not a check that
 *  passed (spec §10, invariant 71). */
export const SEVERITY_FR: Record<string, string> = {
  CRITICAL: 'CRITIQUE',
  MAJOR: 'MAJEUR',
  MINOR: 'MINEUR',
  UNKNOWN: 'INCONNU',
};
