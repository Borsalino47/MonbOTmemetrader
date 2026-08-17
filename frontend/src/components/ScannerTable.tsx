import { useMemo, useState } from 'react';
import { SETUP_STATE_FR, labelFor } from '../i18n/fr';
import type { ScoreRow } from '../types';
import { DASH, maturityColor, mult, num, pct, price, scoreColor, signClass } from '../format';
import { VerdictBadge } from './VerdictBadge';

type SortKey =
  | 'rank' | 'symbol' | 'price' | 'change5m' | 'change1h' | 'rvol'
  | 'final_score' | 'acceleration' | 'pump_maturity' | 'safety' | 'confidence' | 'state' | 'verdict';

interface Props {
  rows: ScoreRow[];
  onSelect: (symbol: string) => void;
}

const COLUMNS: { key: SortKey; label: string; left?: boolean; title?: string }[] = [
  { key: 'rank', label: '#', left: true },
  { key: 'symbol', label: 'Actif', left: true },
  { key: 'price', label: 'Prix' },
  { key: 'change5m', label: '5 min' },
  { key: 'change1h', label: '1 h' },
  { key: 'rvol', label: 'RVOL', title: 'Volume actuel comparé à la moyenne des 50 dernières bougies' },
  { key: 'final_score', label: 'Opportunité', title: 'Score final sur 100 — un classement, pas une probabilité' },
  { key: 'acceleration', label: 'Accél.', title: 'Accélération du momentum, de 0 à 100' },
  { key: 'pump_maturity', label: 'Maturité', title: 'Où en est déjà le mouvement — plus bas signifie plus tôt' },
  { key: 'safety', label: 'Sécurité' },
  { key: 'confidence', label: 'Conf.', title: 'Confiance dans les données, de 0 à 100' },
  { key: 'state', label: 'État', left: true },
  { key: 'verdict', label: 'Verdict', left: true, title: 'Résumé en clair des mêmes filtres — ce n’est pas un conseil' },
];

// Worst first when sorting descending, so the rows worth a look surface together.
const VERDICT_RANK: Record<string, number> = { AVOID: 0, RISKY: 1, WATCH: 2, STRONG: 3 };

function value(row: ScoreRow, key: SortKey, index: number): number | string {
  const m = row.metrics ?? {};
  switch (key) {
    case 'rank': return index;
    case 'symbol': return row.symbol;
    case 'price': return row.price;
    case 'change5m': return m.change_5m_pct ?? -Infinity;
    case 'change1h': return m.change_1h_pct ?? -Infinity;
    case 'rvol': return m.rvol ?? -Infinity;
    case 'final_score': return row.final_score;
    case 'acceleration': return row.acceleration.momentum_acceleration;
    case 'pump_maturity': return row.pump_maturity.score;
    case 'safety': return row.safety.score;
    case 'confidence': return row.data_confidence.score;
    case 'state': return row.setup.state;
    case 'verdict': return VERDICT_RANK[row.verdict?.level] ?? -1;
  }
}

export function ScannerTable({ rows, onSelect }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('rank');
  const [asc, setAsc] = useState(true);

  const sorted = useMemo(() => {
    const indexed = rows.map((r, i) => ({ r, i }));
    indexed.sort((a, b) => {
      const va = value(a.r, sortKey, a.i);
      const vb = value(b.r, sortKey, b.i);
      let cmp: number;
      if (typeof va === 'string' || typeof vb === 'string') cmp = String(va).localeCompare(String(vb));
      else cmp = va - vb;
      return asc ? cmp : -cmp;
    });
    return indexed;
  }, [rows, sortKey, asc]);

  function toggle(key: SortKey) {
    if (key === sortKey) setAsc(!asc);
    else {
      setSortKey(key);
      // Rank and symbol read naturally ascending; every score reads best descending.
      setAsc(key === 'rank' || key === 'symbol');
    }
  }

  if (rows.length === 0) {
    return <div className="table-wrap"><div className="empty">Aucun actif ne correspond aux filtres actuels.</div></div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <th
                key={c.key}
                className={c.left ? 'left' : ''}
                onClick={() => toggle(c.key)}
                title={c.title}
              >
                {c.label}
                {sortKey === c.key && <span className="arrow">{asc ? '↑' : '↓'}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map(({ r, i }) => {
            const m = r.metrics ?? {};
            const c5 = m.change_5m_pct ?? null;
            const c1h = m.change_1h_pct ?? null;
            const rvol = m.rvol ?? null;
            const vetoed = r.safety.hard_veto || r.liquidity.veto;
            return (
              <tr key={r.symbol} className={vetoed ? 'vetoed' : ''} onClick={() => onSelect(r.symbol)}>
                <td className="left rank">{i + 1}</td>
                <td className="sym">
                  {r.symbol}
                  {vetoed && <span className="veto-flag" title="Veto absolu : liquidité ou sécurité">VETO</span>}
                </td>
                <td>{price(r.price)}</td>
                <td className={signClass(c5)}>{pct(c5)}</td>
                <td className={signClass(c1h)}>{pct(c1h)}</td>
                <td className={rvol && rvol >= 2 ? 'pos' : ''}>{rvol === null ? DASH : mult(rvol, 2)}</td>
                <td>
                  <span className="scorebar" style={{ color: scoreColor(r.final_score) }}>
                    {num(r.final_score, 1)}
                    <span className="fill" style={{ width: `${r.final_score}%`, background: scoreColor(r.final_score) }} />
                  </span>
                  {r.score_acceleration !== null && Math.abs(r.score_acceleration) >= 1 && (
                    <span className={signClass(r.score_acceleration)} style={{ fontSize: 10, marginLeft: 4 }}>
                      {r.score_acceleration > 0 ? '▲' : '▼'}{num(Math.abs(r.score_acceleration), 0)}
                    </span>
                  )}
                </td>
                <td>{num(r.acceleration.momentum_acceleration, 0)}</td>
                <td style={{ color: maturityColor(r.pump_maturity.score) }}>{num(r.pump_maturity.score, 0)}</td>
                <td>{num(r.safety.score, 0)}</td>
                <td className={r.data_confidence.score < 60 ? 'neg' : ''}>{num(r.data_confidence.score, 0)}</td>
                <td className="left">
                  <span className={`pill ${r.setup.state}`}>{labelFor(SETUP_STATE_FR, r.setup.state)}</span>
                </td>
                <td className="left">
                  {r.verdict && <VerdictBadge verdict={r.verdict} size="sm" />}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
