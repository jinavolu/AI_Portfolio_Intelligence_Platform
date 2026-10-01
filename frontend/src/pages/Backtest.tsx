import { useEffect, useRef, useState } from "react";
import { ShadowComparison } from "../News";
import { api, fmt, useApi } from "../api";

type Criteria = { locked_at: string; set_by: string; criteria: Record<string, unknown>; definitions: Record<string, string>; sha256: string };
type DataStatus = {
  job: { running: boolean; universe?: string; done?: number; total?: number | null; skipped?: number; failed?: Record<string, string>; error?: string | null };
  stored: { universe: string; years: number; fetched_on: string; missing: string[]; members: number } | null;
};
type RunSummary = { id: number; created_at: string; window: "development" | "holdout"; rules_version: string; horizon: string; passed: boolean; oos_excess_cagr: number | null; universe: string };
type Holdout = { sealed_from: string; locked_at: string; rules_of_use: string[]; criteria: Record<string, unknown>; eligible: Record<string, string | null> };
type Side = { cagr: number | null; max_drawdown: number; sharpe: number | null; sortino: number | null; final_value: number };
type Period = {
  start: string; end: string; years: number; strategy: Side; benchmark: Side; excess_cagr: number | null;
  trades: number; win_rate: number | null; profit_factor: number | null; turnover_per_year: number | null;
  equal_weight_hold: { cagr: number | null; max_drawdown: number; final_value: number } | null;
};
type Job = { running: boolean; horizon?: string; window?: string; error?: string | null; run_id?: number | null };
type Unit = "fraction" | "count" | "regimes";
type Check = { criterion: string; value: unknown; threshold: unknown; passed: boolean; unit?: Unit };

/** A check's value in its own unit (runs stored before `unit` existed: read from the criterion's wording). */
function checkValue(c: Check, v: unknown): string {
  const unit: Unit = c.unit ?? (/trades/i.test(c.criterion) ? "count" : /regime/i.test(c.criterion) ? "regimes" : "fraction");
  if (v == null) return "—";
  if (unit === "regimes" && typeof v === "object")
    return Object.entries(v as Record<string, boolean>).map(([k, ok]) => `${k} ${ok ? "✓" : "✗"}`).join(", ") || "—";
  if (typeof v !== "number") return String(v);
  return unit === "fraction" ? fmt.pct(v) : String(v);
}
type Run = {
  id: number; created_at: string; window: "development" | "holdout"; sealed_from: string; rules_version: string; horizon: string; passed: boolean; criteria_locked_at: string;
  universe: { name: string; size: number; missing: string[] };
  periods: { full?: Period; in_sample?: Period; out_of_sample?: Period; holdout?: Period };
  oos_start: string; checks: Check[]; taxes_paid: number;
  regimes: { year: number; regime: string; strategy: number; benchmark: number }[];
  equity_curve: { date: string; strategy: number; benchmark: number; equal_weight: number | null }[];
  limitations: string[];
  accounting: { difference: number };
};

const HORIZONS = ["LONG_TERM", "MEDIUM_TERM", "SHORT_TERM"];

export default function Backtest() {
  const [universe, setUniverse] = useState("nifty100");
  const [years, setYears] = useState(10);
  const [msg, setMsg] = useState<string>();
  // The opened run is part of the url: clicking runs quickly can't show one run under another's row.
  const [runId, setRunId] = useState<number | null>(null);

  const criteria = useApi<Criteria>("/api/backtest/criteria").data;
  // Download and run jobs poll only while they're running (and keep polling through a failed reply).
  const dataApi = useApi<DataStatus>("/api/backtest/data", { poll: (d) => (d?.job.running ? 1500 : null) });
  const jobApi = useApi<Job>("/api/backtest/job", { poll: (j) => (j?.running ? 1500 : null) });
  const runsApi = useApi<RunSummary[]>("/api/backtest/runs");
  const holdoutApi = useApi<Holdout>("/api/backtest/holdout");
  const validatedApi = useApi<{ current_rules: string; validated: string[] }>("/api/backtest/validated");
  const run = useApi<Run>(runId == null ? null : `/api/backtest/runs/${runId}`).data;
  const data = dataApi.data, job = jobApi.data ?? { running: false }, runs = runsApi.data ?? [];
  const holdout = holdoutApi.data, validated = validatedApi.data;
  const loadValidated = validatedApi.reload;
  const openRun = (id: number) => setRunId(id);
  const progressErr = dataApi.error ?? jobApi.error;

  // The finished run opens once, when the run job goes from running to done; a history download (or
  // an older run) never replaces the run you opened.
  const runWasRunning = useRef(false);
  useEffect(() => {
    if (runWasRunning.current && !job.running && job.run_id) {
      runsApi.reload();
      holdoutApi.reload();
      setRunId(job.run_id);
    }
    runWasRunning.current = job.running;
  }, [job.running, job.run_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const fetchHistory = () =>
    api("/api/backtest/data", { method: "POST", body: JSON.stringify({ universe, years }) })
      .then(() => dataApi.reload()).catch((e) => setMsg((e as Error).message));
  const startRun = (horizon: string, window: "development" | "holdout" = "development") =>
    api<Job>("/api/backtest/run", { method: "POST", body: JSON.stringify({ horizon, window }) })
      .then((j) => { runWasRunning.current = true; jobApi.setData(() => j); jobApi.reload(); })
      .catch((e) => setMsg((e as Error).message));
  const validate = (id: number) =>
    api<{ validated: string }>(`/api/backtest/runs/${id}/validate`, { method: "POST" })
      .then((r) => { setMsg(`Marked ${r.validated} as validated`); loadValidated(); })
      .catch((e) => setMsg((e as Error).message));
  const revoke = (entry: string) =>
    api(`/api/backtest/validated/${encodeURIComponent(entry)}`, { method: "DELETE" }).then(loadValidated)
      .catch((e) => setMsg((e as Error).message));

  return (
    <>
      <section className="card">
        <h2>Backtesting</h2>
        <p className="muted small">
          Tests the deterministic signal rules on history against Nifty 50 buy-and-hold, after costs and taxes. Sequence:
          <b> development window</b> (before {holdout?.sealed_from ?? "the sealed date"}) → if it passes, <b>one</b> run on the
          <b> sealed holdout</b> → if that passes too, you may mark the rule version validated. Only then do BUY/SELL become
          <b> candidates</b> instead of signals. Passing means meeting criteria fixed in advance, not a guarantee of future returns.
        </p>
        {msg && <p className="notice">{msg} <button className="secondary" onClick={() => setMsg(undefined)}>✕</button></p>}
        {progressErr && <p className="error small">Couldn't refresh (retrying while a job runs): {progressErr}</p>}
      </section>

      <ShadowComparison />

      <div className="grid2">
        <section className="card">
          <h2>Pass criteria <span className="muted small">locked {criteria && new Date(criteria.locked_at).toLocaleString()}</span></h2>
          {criteria && (
            <>
              <ul className="small">
                {Object.entries(criteria.definitions).filter(([k]) => k in criteria.criteria).map(([k, v]) => (
                  <li key={k}><b>{k.replace(/_/g, " ")}</b> = {String(criteria.criteria[k])}: {v}</li>
                ))}
              </ul>
              <details className="small">
                <summary>How costs, benchmark and out-of-sample are defined</summary>
                <ul>
                  {["benchmark", "after_costs", "out_of_sample"].map((k) => <li key={k}><b>{k.replace(/_/g, " ")}</b>: {criteria.definitions[k]}</li>)}
                </ul>
              </details>
              <p className="muted small">File hash {criteria.sha256.slice(0, 12)}… · a run can only validate rules if this hash is unchanged.</p>
            </>
          )}
        </section>

        <section className="card">
          <h2>1. History</h2>
          {data?.stored ? (
            <p className="small">
              Stored: <b>{data.stored.universe}</b>, {data.stored.members} stocks + Nifty 50, {data.stored.years} years,
              fetched {data.stored.fetched_on}
              {data.stored.missing.length > 0 && <span className="warn"> · {data.stored.missing.length} failed: {data.stored.missing.slice(0, 8).join(", ")}</span>}
            </p>
          ) : <p className="muted small">No history downloaded yet.</p>}
          <div className="filters">
            <select value={universe} onChange={(e) => setUniverse(e.target.value)} disabled={data?.job.running}>
              <option value="nifty50">Nifty 50 (~1 min)</option>
              <option value="nifty100">Nifty 100 (~2 min)</option>
              <option value="nifty500">Nifty 500 (~8 min)</option>
            </select>
            <select value={years} onChange={(e) => setYears(Number(e.target.value))} disabled={data?.job.running}>
              {[10, 12, 15].map((y) => <option key={y} value={y}>{y} years</option>)}
            </select>
            <button onClick={fetchHistory} disabled={data?.job.running}>{data?.job.running ? "Downloading…" : "Download history"}</button>
          </div>
          {data?.job.running && (
            <p className="small">
              {data.job.done ?? 0} / {data.job.total ?? "…"} symbols
              {data.job.skipped ? ` · ${data.job.skipped} already up to date` : ""}
              {data.job.failed && Object.keys(data.job.failed).length ? ` · ${Object.keys(data.job.failed).length} failed` : ""}
            </p>
          )}
          {data?.job.error && <p className="error">{data.job.error}</p>}
          <p className="muted small">
            Uses your logged-in Kite session (≈3 requests/second). Index lists are today's NSE constituents. At least 10 years:
            the development window ends at the {holdout?.sealed_from ?? "seal"} date and needs ~220 warm-up sessions plus several full years.
          </p>

          <h2>2. Run on the development window</h2>
          <div className="filters">
            {HORIZONS.map((h) => (
              <button key={h} disabled={job.running || !data?.stored || data?.job.running} onClick={() => startRun(h)}>
                {job.running && job.horizon === h && job.window !== "holdout" ? "Running…" : `Backtest ${h.replace("_", " ").toLowerCase()}`}
              </button>
            ))}
          </div>

          <h2>3. Sealed holdout <span className="muted small">from {holdout?.sealed_from} · locked {holdout && new Date(holdout.locked_at).toLocaleDateString()}</span></h2>
          <p className="muted small">
            Once per rule version and horizon, only after the development run passes. Needs: excess CAGR ≥{" "}
            {holdout ? fmt.pct(holdout.criteria.min_excess_cagr as number) : "…"}, drawdown no worse than Nifty,
            ≥ {String(holdout?.criteria.min_trades ?? "…")} trades.
          </p>
          <div className="filters">
            {HORIZONS.map((h) => {
              const why = holdout?.eligible[h];
              return (
                <button key={h} className="secondary" title={why ?? "Eligible: runs once, result is final"}
                        disabled={!!why || job.running || !data?.stored || data?.job.running}
                        onClick={() => { if (confirm(`Use the sealed holdout for ${h}? This can be done only once for this rule version.`)) startRun(h, "holdout"); }}>
                  {job.running && job.horizon === h && job.window === "holdout" ? "Running…" : `Holdout ${h.replace("_", " ").toLowerCase()}`}
                </button>
              );
            })}
          </div>
          {holdout && Object.values(holdout.eligible).every(Boolean) && (
            <p className="muted small">Locked for every horizon: {holdout.eligible.LONG_TERM}</p>
          )}
          {job.error && <p className="error">{job.error}</p>}
        </section>
      </div>

      <div className="grid2">
        <section className="card">
          <h2>Runs</h2>
          {runs.length === 0 && <p className="muted">No runs yet.</p>}
          <table>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className={run?.id === r.id ? "selected" : ""} style={{ cursor: "pointer" }} onClick={() => openRun(r.id)}>
                  {/* The row stays clickable; the date is also a button, so a run can be opened from the keyboard. */}
                  <td className="small">
                    <button className="th-sort" aria-current={run?.id === r.id} onClick={(e) => { e.stopPropagation(); openRun(r.id); }}>
                      {new Date(r.created_at).toLocaleString()}
                    </button>
                  </td>
                  <td>{r.horizon}</td>
                  <td>{r.window === "holdout" ? <span className="tag">holdout</span> : <span className="muted small">development</span>}</td>
                  <td className="small muted">{r.universe} · {r.rules_version}</td>
                  <td className={`num ${fmt.sign(r.oos_excess_cagr)}`}>{fmt.pct(r.oos_excess_cagr)}</td>
                  <td>{r.passed ? <span className="signal s-HOLD">PASS</span> : <span className="signal s-SELL_SIGNAL">FAIL</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h2>Validated rule sets</h2>
          <p className="small">App uses <b>{validated?.current_rules}</b>.</p>
          {validated?.validated.length ? (
            <ul>{validated.validated.map((v) => <li key={v}>{v} <button className="secondary" onClick={() => revoke(v)}>Revoke</button></li>)}</ul>
          ) : <p className="muted small">None: every BUY/SELL is shown as a signal, not a candidate.</p>}
        </section>
      </div>

      {run && <RunDetail run={run} onValidate={() => validate(run.id)} />}
    </>
  );
}

function RunDetail({ run, onValidate }: { run: Run; onValidate: () => void }) {
  const P = run.periods;
  const isHoldout = run.window === "holdout";
  const cols: [string, Period | undefined][] = isHoldout
    ? [["Sealed holdout", P.holdout]]
    : [["Full", P.full], ["In-sample", P.in_sample], ["Out-of-sample", P.out_of_sample]];
  const rows: [string, (p: Period) => string][] = [
    ["Period", (p) => `${p.start} → ${p.end} (${p.years} y)`],
    ["Strategy CAGR", (p) => fmt.pct(p.strategy.cagr)],
    ["Nifty 50 CAGR", (p) => fmt.pct(p.benchmark.cagr)],
    ["Excess CAGR vs Nifty", (p) => fmt.pct(p.excess_cagr)],
    ["Equal-weight hold CAGR (same stocks)", (p) => fmt.pct(p.equal_weight_hold?.cagr)],
    ["Strategy max drawdown", (p) => fmt.pct(p.strategy.max_drawdown)],
    ["Nifty 50 max drawdown", (p) => fmt.pct(p.benchmark.max_drawdown)],
    ["Equal-weight hold max drawdown", (p) => fmt.pct(p.equal_weight_hold?.max_drawdown)],
    ["Sharpe (strategy / Nifty)", (p) => `${p.strategy.sharpe ?? "—"} / ${p.benchmark.sharpe ?? "—"}`],
    ["Sortino (strategy / Nifty)", (p) => `${p.strategy.sortino ?? "—"} / ${p.benchmark.sortino ?? "—"}`],
    ["Closed trades", (p) => String(p.trades)],
    ["Win rate", (p) => fmt.pct(p.win_rate)],
    ["Profit factor", (p) => String(p.profit_factor ?? "—")],
    ["Turnover / year", (p) => (p.turnover_per_year == null ? "—" : `${p.turnover_per_year}×`)],
  ];
  return (
    <section className="card">
      <div className="card-head">
        <h2>
          Run {run.id}: {run.horizon} · {isHoldout ? <span className="tag">sealed holdout</span> : "development window"} ·{" "}
          {run.universe.name} ({run.universe.size} stocks) · {run.rules_version}
        </h2>
        {run.passed && isHoldout && <button onClick={onValidate}>Mark {run.rules_version}:{run.horizon} validated</button>}
        {run.passed && !isHoldout && <span className="signal s-HOLD">Passed development: next, the sealed holdout (once)</span>}
        {!run.passed && <span className="signal s-SELL_SIGNAL">Did not pass: rules stay as signals</span>}
      </div>

      <h3>{isHoldout ? `Holdout criteria (sealed from ${run.sealed_from})` : `Development criteria (judged on out-of-sample from ${run.oos_start}, before the seal on ${run.sealed_from})`}</h3>
      <ul className="checks">
        {run.checks.map((c) => (
          <li key={c.criterion} className={c.passed ? "pos" : "neg"}>
            {c.passed ? "✓" : "✗"} {c.criterion}: <b>{checkValue(c, c.value)}</b>
            {typeof c.threshold === "number" && (
              <span className="muted"> (needs {c.unit === "regimes" || /regime/i.test(c.criterion) ? `${c.threshold} regime types` : checkValue(c, c.threshold)})</span>
            )}
          </li>
        ))}
      </ul>

      <EquityChart curve={run.equity_curve} bandFrom={isHoldout ? null : run.oos_start} />

      <div className="scroll">
        <table>
          <thead><tr><th />{cols.map(([label]) => <th key={label} className="num">{label}</th>)}</tr></thead>
          <tbody>
            {rows.map(([label, f]) => (
              <tr key={label}>
                <td>{label}</td>
                {cols.map(([c, p], i) => <td key={c} className="num">{p ? (i === cols.length - 1 ? <b>{f(p)}</b> : f(p)) : "—"}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Equal-weight hold = the same stocks bought in equal amounts on day one and held (a second baseline, not a criterion).
        Taxes paid: {fmt.inr(run.taxes_paid)} · cash reconciles with the trade ledger to {run.accounting.difference}.
      </p>

      {run.regimes.length > 0 && (
        <>
          <h3>Calendar years by market regime</h3>
          <div className="scroll">
            <table>
              <thead><tr><th>Year</th><th>Regime (by Nifty)</th><th className="num">Strategy</th><th className="num">Nifty 50</th></tr></thead>
              <tbody>
                {run.regimes.map((r) => (
                  <tr key={r.year}><td>{r.year}</td><td>{r.regime}</td>
                    <td className={`num ${r.strategy >= r.benchmark ? "pos" : "neg"}`}>{fmt.pct(r.strategy)}</td>
                    <td className="num">{fmt.pct(r.benchmark)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <h3>Known limitations</h3>
      <ul className="small muted">{run.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
    </section>
  );
}

function EquityChart({ curve, bandFrom }: { curve: Run["equity_curve"]; bandFrom: string | null }) {
  if (curve.length < 2) return null;
  const W = 900, H = 240, P = 36;
  const vals = curve.flatMap((c) => [c.strategy, c.benchmark, ...(c.equal_weight != null ? [c.equal_weight] : [])]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const x = (i: number) => P + (i / (curve.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - min) / (max - min || 1)) * (H - 2 * P);
  // A missing point breaks the line (a gap): it is never drawn at another series' value.
  const line = (k: "strategy" | "benchmark" | "equal_weight") => {
    let pen = "M";
    return curve.map((c, i) => {
      const v = c[k];
      if (v == null) { pen = "M"; return ""; }
      const seg = `${pen}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = "L";
      return seg;
    }).join("");
  };
  // No band when the out-of-sample start isn't on the curve (findIndex -1 would shade all of it).
  const bandI = bandFrom ? curve.findIndex((c) => c.date >= bandFrom) : -1;
  const hasEw = curve.some((c) => c.equal_weight != null);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Equity curve: strategy, Nifty 50 and equal-weight hold">
      {bandI >= 0 && <>
        <rect x={x(bandI)} y={P / 2} width={W - P - x(bandI)} height={H - 1.5 * P} className="oos-band" />
        <text x={x(bandI) + 4} y={P / 2 + 12} className="axis">out-of-sample</text>
      </>}
      {hasEw && <path d={line("equal_weight")} fill="none" stroke="var(--warn)" strokeWidth="1.2" strokeDasharray="1 3" />}
      <path d={line("benchmark")} fill="none" stroke="var(--muted)" strokeWidth="1.5" strokeDasharray="4 3" />
      <path d={line("strategy")} fill="none" stroke="var(--accent)" strokeWidth="2" />
      <text x={P} y={14} className="axis">{fmt.inr(max)}</text>
      <text x={P} y={H - 8} className="axis">
        {fmt.inr(min)} · {curve[0].date} → {curve[curve.length - 1].date} · solid = strategy, dashed = Nifty 50{hasEw ? ", dotted = equal-weight hold" : ""}
      </text>
    </svg>
  );
}
