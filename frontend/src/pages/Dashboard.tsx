import { useState } from "react";
import { api, errorText, fmt, useApi } from "../api";

type Portfolio = {
  created_at: string;
  data_as_of: Record<string, string | null>;
  versions: Record<string, string>;
  rule_set_validated: boolean;
  summary: {
    total_invested: number;
    total_current_value: number;
    total_pnl: number;
    total_pnl_pct: number | null;
    day_change: number | null;
    day_change_pct: number | null;
    xirr: number | null;
    sector_allocation: Record<string, number>;
  };
  risk: { breaches: string[]; top5_weight: number; herfindahl_index: number };
  attention: { symbol: string; state: string; reason: string; summary?: string }[];
};

type Diff = {
  holdings: Record<string, { field: string; from: unknown; to: unknown; change?: number }[]>;
  rules_changed: boolean;
  from_created_at: string;
  to_created_at: string;
  added_holdings: string[];
  removed_holdings: string[];
  portfolio: { value_from: number | null; value_to: number | null; value_change: number | null };
};
type SchedulerStatus = { snapshot_time_ist: string; taken_today: boolean; taken_at: string | null; due: boolean; last_error: string | null };

const RANGES = [
  { days: 0, label: "Previous snapshot" },
  { days: 1, label: "1 day" },
  { days: 7, label: "1 week" },
  { days: 30, label: "1 month" },
];

export default function Dashboard() {
  const [range, setRange] = useState(7);
  const [actionErr, setActionErr] = useState<string>();
  const portfolio = useApi<Portfolio>("/api/portfolio");
  const scheduler = useApi<SchedulerStatus>("/api/scheduler");
  // The range is part of the url: switching quickly can't show one range's result under another.
  const changes = useApi<Diff>(`/api/snapshots/diff${range ? `?days=${range}` : ""}`);
  const p = portfolio.data, sched = scheduler.data, diff = changes.data;
  const diffErr = actionErr ?? changes.error;
  const load = () => { setActionErr(undefined); portfolio.reload(); scheduler.reload(); changes.reload(); };

  if (!p) return portfolio.error ? <p className="error">{portfolio.error}</p> : <p>Loading…</p>;
  const s = p.summary;

  return (
    <>
      <section className="tiles">
        <Tile label="Portfolio value" value={fmt.inr(s.total_current_value)} sub={`Invested ${fmt.inr(s.total_invested)}`} />
        <Tile label="Today's P&L" value={fmt.inr(s.day_change)} sub={fmt.pct(s.day_change_pct)} tone={fmt.sign(s.day_change)} />
        <Tile label="Overall P&L" value={fmt.inr(s.total_pnl)} sub={fmt.pct(s.total_pnl_pct)} tone={fmt.sign(s.total_pnl)} />
        <Tile label="XIRR" value={fmt.pct(s.xirr)} sub={s.xirr == null ? "needs reconciled tradebook" : "money-weighted"} />
      </section>

      <div className="grid2">
        <section className="card">
          <h2>Needs attention</h2>
          {p.attention.length === 0 && <p className="muted">Nothing needs attention.</p>}
          <ul className="attention">
            {p.attention.map((a) => (
              <li key={a.symbol}>
                <a href={`#/stock/${a.symbol}`}>{a.symbol}</a> <Signal state={a.state} />
                <div className="muted" title={`Rule detail: ${a.reason}`}>{a.summary || a.reason}</div>
              </li>
            ))}
          </ul>
        </section>

        <section className="card">
          <h2>Sector allocation</h2>
          {Object.entries(s.sector_allocation)
            .sort((a, b) => b[1] - a[1])
            .map(([sector, w]) => (
              <div key={sector} className="bar-row">
                <span>{sector}</span>
                <div className="bar"><div style={{ width: `${w * 100}%` }} /></div>
                <span className="num">{fmt.pct(w)}</span>
              </div>
            ))}
          {p.risk.breaches.length > 0 && (
            <>
              <h3>Concentration limits</h3>
              <ul>{p.risk.breaches.map((b) => <li key={b} className="warn">{b}</li>)}</ul>
            </>
          )}
        </section>
      </div>

      <section className="card">
        <div className="card-head">
          <h2>What changed</h2>
          <div className="seg">
            {RANGES.map((r) => (
              <button key={r.days} className={range === r.days ? "on" : ""}
                      onClick={() => { setActionErr(undefined); setRange(r.days); }}>{r.label}</button>
            ))}
          </div>
        </div>
        {sched && (
          <p className="muted small">
            Daily snapshot ({sched.snapshot_time_ist} IST, weekdays):{" "}
            {sched.taken_today ? `taken today at ${new Date(sched.taken_at!).toLocaleTimeString()}`
              : sched.due ? `due, will retry${sched.last_error ? `: ${sched.last_error}` : ""}` : "not yet due today"}
          </p>
        )}
        {diffErr && (
          <p className="muted">
            {diffErr}{" "}
            {range === 0 && (
              <button onClick={() => api("/api/snapshots", { method: "POST" }).then(load)
                .catch((e) => setActionErr(`Couldn't take a snapshot: ${(e as Error).message}`))}>Take snapshot now</button>
            )}
          </p>
        )}
        {diff && (
          <>
            <p className="muted small">
              {new Date(diff.from_created_at).toLocaleString()} → {new Date(diff.to_created_at).toLocaleString()} ·
              portfolio {fmt.inr(diff.portfolio.value_from)} → {fmt.inr(diff.portfolio.value_to)}{" "}
              <span className={fmt.sign(diff.portfolio.value_change)}>({fmt.inr(diff.portfolio.value_change)})</span>
            </p>
            {diff.rules_changed && <p className="warn">Rule version changed between these snapshots: some differences come from the rules, not the data.</p>}
            {diff.added_holdings.length > 0 && <p>New holdings: {diff.added_holdings.join(", ")}</p>}
            {diff.removed_holdings.length > 0 && <p>Exited: {diff.removed_holdings.join(", ")}</p>}
            {Object.keys(diff.holdings).length === 0 && <p className="muted">No changes.</p>}
            <ChangeTable holdings={diff.holdings} />
          </>
        )}
      </section>

      <section className="card">
        <h2>Tradebook</h2>
        <p className="muted small">
          Upload the Console tradebook CSV (Console → Reports → Tradebook → Equity, all years). It supplies buy dates for
          lots, tax terms and XIRR. It replaces any previously uploaded tradebook.
        </p>
        <input type="file" accept=".csv" aria-label="Tradebook CSV" onChange={async (e) => {
          const input = e.target;
          const file = input.files?.[0];
          if (!file) return;
          const form = new FormData();
          form.append("file", file);
          try {
            const res = await fetch("/api/tradebook", { method: "POST", body: form });
            const body = await res.json().catch(() => ({}));
            alert(res.ok ? `Imported ${body.imported} trades`
              : `Import failed: ${errorText(body.detail) ?? `${res.status} ${res.statusText}`}`);
            if (res.ok) load();
          } catch (err) {
            alert(`Import failed: ${(err as Error).message}`);
          } finally {
            input.value = ""; // picking the same file again (after fixing it) fires onChange again
          }
        }} />
      </section>

      <footer className="muted">
        Snapshot {new Date(p.created_at).toLocaleString()} · holdings as of {p.data_as_of.holdings} · candles as of{" "}
        {p.data_as_of.candles} · {Object.values(p.versions).join(", ")} ·{" "}
        {p.rule_set_validated ? "rules VALIDATED" : "rules not validated: BUY/SELL shown as signals"}
      </footer>
    </>
  );
}

/** Meaningful changes first (signal, trend, gates, thesis); plain price moves are summarised as one column. */
function ChangeTable({ holdings }: { holdings: Diff["holdings"] }) {
  const rows = Object.entries(holdings).map(([sym, items]) => {
    const price = items.find((i) => i.field === "price_change_pct");
    const other = items.filter((i) => i.field !== "price_change_pct");
    return { sym, price: price?.change, other };
  }).sort((a, b) => b.other.length - a.other.length || Math.abs(b.price ?? 0) - Math.abs(a.price ?? 0));
  if (rows.length === 0) return null;
  const show = (v: unknown) => (Array.isArray(v) ? (v.length ? v.join(",") : "none") : String(v ?? "—"));
  return (
    <div className="scroll">
      <table>
        <thead><tr><th>Stock</th><th className="num">Price</th><th>Other changes</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.sym}>
              <td><a href={`#/stock/${r.sym}`}>{r.sym}</a></td>
              <td className={`num ${fmt.sign(r.price)}`}>{fmt.pct(r.price)}</td>
              <td className="small">
                {r.other.length === 0 ? <span className="muted">—</span> : r.other.map((i) => (
                  <div key={i.field}><b>{i.field.replace(/_/g, " ")}</b>: {show(i.from)} → {show(i.to)}</div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="tile">
      <div className="muted">{label}</div>
      <div className={`big ${tone ?? ""}`}>{value}</div>
      {sub && <div className={`muted ${tone ?? ""}`}>{sub}</div>}
    </div>
  );
}

export function Signal({ state, dontAdd }: { state: string; dontAdd?: boolean }) {
  return (
    <>
      <span className={`signal s-${state}`}>{state.replace("_", " ")}</span>
      {dontAdd && state !== "DONT_ADD" && <span className="signal s-DONT_ADD">DON'T ADD</span>}
    </>
  );
}
