import { useEffect, useMemo, useState } from "react";
import { fmt, Holding, useApi } from "../api";
import BulkThesis from "../BulkThesis";
import { Signal } from "./Dashboard";

type SortKey = "symbol" | "sector" | "quantity" | "last_price" | "day_change_pct" | "pnl" | "pnl_pct" | "weight" | "signal";
type NumKey = "quantity" | "last_price" | "day_change_pct" | "pnl" | "pnl_pct" | "weight";
type Filters = {
  q: string; signal: string; sector: string; trend: string; status: string; thesis: string; reason: string;
  num: Partial<Record<NumKey, string>>;
};

const EMPTY: Filters = { q: "", signal: "", sector: "", trend: "", status: "", thesis: "", reason: "", num: {} };
const STORAGE_KEY = "holdings-filters-v2";
// Fractions shown as percentages: the user filters in percent (">5" means above 5%).
const PERCENT_COLS = new Set<NumKey>(["day_change_pct", "pnl_pct", "weight"]);
const NUM_PLACEHOLDER: Record<NumKey, string> = {
  quantity: ">10", last_price: ">500", day_change_pct: "<0", pnl: ">0", pnl_pct: "<-10", weight: ">2",
};

function thesisState(h: Holding): "missing" | "draft" | "complete" {
  return !h.thesis ? "missing" : h.thesis.draft ? "draft" : "complete";
}

const SIGNAL_LABELS: Record<string, string> = {
  BUY_SIGNAL: "Buy signal", SELL_SIGNAL: "Sell signal", REVIEW: "Review", HOLD: "Hold", DONT_ADD: "Don't add",
  NO_ACTION: "No action", BUY_CANDIDATE: "Buy candidate", SELL_CANDIDATE: "Sell candidate",
};

/** Why a holding has its signal, grouped so the owner can see what to do next. */
function statusOf(h: Holding): string {
  if (h.reason.startsWith("Thesis missing") || h.reason.startsWith("Thesis has no horizon")) return "needs_thesis";
  if (h.reason.startsWith("Required data") || h.reason.startsWith("Stale")) return "data_issue";
  if (h.signal === "REVIEW") return "review";
  if (h.signal.startsWith("BUY") || h.signal.startsWith("SELL") || h.signal === "DONT_ADD" || h.dont_add) return "signal";
  return "other";
}

const STATUS_LABELS: Record<string, string> = {
  needs_thesis: "Needs thesis",
  data_issue: "Data missing / stale",
  review: "Needs review",
  signal: "Buy / sell / don't-add signal",
  other: "Hold / neutral",
};

/** Numeric column filter: ">5", ">=5", "<0", "<=-10", "=100", "5..10" (range), or a bare number (≥).
 *  Returns null when empty, "invalid" when it can't be parsed. */
function parseNumFilter(expr: string | undefined): ((v: number) => boolean) | null | "invalid" {
  const s = (expr ?? "").replace(/[,%₹\s]/g, "");
  if (!s) return null;
  const range = s.match(/^(-?\d*\.?\d+)\.\.(-?\d*\.?\d+)$/);
  if (range) {
    const [lo, hi] = [Number(range[1]), Number(range[2])].sort((a, b) => a - b);
    return (v) => v >= lo && v <= hi;
  }
  const m = s.match(/^(>=|<=|>|<|=)?(-?\d*\.?\d+)$/);
  if (!m) return "invalid";
  const n = Number(m[2]);
  switch (m[1]) {
    case ">": return (v) => v > n;
    case "<": return (v) => v < n;
    case "<=": return (v) => v <= n;
    case "=": return (v) => Math.abs(v - n) < 1e-9;
    default: return (v) => v >= n;
  }
}

function loadFilters(): Filters {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    return { ...EMPTY, ...saved, num: { ...(saved.num ?? {}) } };
  } catch {
    return EMPTY;
  }
}

export default function Holdings() {
  const { data: rows, error: err, reload: load } = useApi<Holding[]>("/api/holdings");
  const [f, setF] = useState<Filters>(loadFilters);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "weight", dir: -1 });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const [notice, setNotice] = useState<string>();

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(f)); } catch { /* storage unavailable */ }
  }, [f]);

  const options = useMemo(() => {
    const uniq = (xs: (string | null)[]) => [...new Set(xs.filter((x): x is string => !!x))].sort();
    // Every signal the rules can produce, always listed (with how many holdings have it now), plus any
    // other state that appears.
    const present = uniq((rows ?? []).map((r) => r.signal));
    return {
      signals: [...new Set([...Object.keys(SIGNAL_LABELS), ...present])].map((s) => ({
        value: s, label: `${SIGNAL_LABELS[s] ?? s} (${(rows ?? []).filter((r) => r.signal === s).length})`,
      })),
      sectors: uniq((rows ?? []).map((r) => r.sector)),
      trends: uniq((rows ?? []).map((r) => r.trend)),
    };
  }, [rows]);

  const numPreds = useMemo(() => {
    const out: Partial<Record<NumKey, ((v: number) => boolean) | "invalid">> = {};
    (Object.keys(f.num) as NumKey[]).forEach((k) => {
      const p = parseNumFilter(f.num[k]);
      if (p) out[k] = p;
    });
    return out;
  }, [f.num]);

  const visible = useMemo(() => {
    if (!rows) return [];
    const q = f.q.trim().toUpperCase();
    const reason = f.reason.trim().toLowerCase();
    const out = rows.filter((h) => {
      if (q && !h.symbol.includes(q) && !h.sector.toUpperCase().includes(q)) return false;
      if (f.signal && h.signal !== f.signal) return false;
      if (f.sector && h.sector !== f.sector) return false;
      if (f.trend && (f.trend === "NONE" ? !!h.trend : h.trend !== f.trend)) return false;
      if (f.status && statusOf(h) !== f.status) return false;
      if (f.thesis && thesisState(h) !== f.thesis) return false;
      if (reason && !`${h.summary} ${h.reason}`.toLowerCase().includes(reason)) return false;
      for (const [k, pred] of Object.entries(numPreds) as [NumKey, ((v: number) => boolean) | "invalid"][]) {
        if (pred === "invalid") continue;
        const raw = h[k];
        if (raw == null) return false; // missing stays missing: it never matches a numeric condition
        if (!pred(PERCENT_COLS.has(k) ? raw * 100 : raw)) return false;
      }
      return true;
    });
    return out.sort((a, b) => {
      const av = a[sort.key] ?? -Infinity, bv = b[sort.key] ?? -Infinity;
      return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
    });
  }, [rows, f, sort, numPreds]);

  if (!rows) return err ? <p className="error">{err}</p> : <p>Loading…</p>;

  const counts = rows.reduce<Record<string, number>>((acc, h) => {
    const s = statusOf(h);
    acc[s] = (acc[s] ?? 0) + 1;
    return acc;
  }, {});
  const set = (k: Exclude<keyof Filters, "num">) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const setNum = (k: NumKey) => (e: { target: { value: string } }) => setF({ ...f, num: { ...f.num, [k]: e.target.value } });
  // The header's sort control is a button inside the cell, so it works from the keyboard too.
  const th = (key: SortKey, label: string, num = false) => (
    <th className={`sortable ${num ? "num" : ""}`}
        aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button className="th-sort" onClick={() => setSort({ key, dir: sort.key === key ? (-sort.dir as 1 | -1) : num ? -1 : 1 })}>
        {label}{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );
  const numFilter = (k: NumKey) => (
    <th>
      <input className={`col-filter num ${numPreds[k] === "invalid" ? "invalid" : ""}`} value={f.num[k] ?? ""}
             onChange={setNum(k)} placeholder={NUM_PLACEHOLDER[k]} aria-label={`Filter ${k.replace(/_/g, " ")}`}
             title={'Examples: >5  <0  >=10  5..20' + (PERCENT_COLS.has(k) ? " (in %)" : "")} />
    </th>
  );
  const active = [f.q, f.signal, f.sector, f.trend, f.status, f.thesis, f.reason].some(Boolean) ||
    Object.values(f.num).some(Boolean);
  const totalValue = visible.reduce((s, h) => s + h.quantity * h.last_price, 0);
  const allVisibleSelected = visible.length > 0 && visible.every((h) => selected.has(h.symbol));
  const toggle = (sym: string) => {
    const next = new Set(selected);
    next.has(sym) ? next.delete(sym) : next.add(sym);
    setSelected(next);
  };
  const toggleAllVisible = () => {
    const next = new Set(selected);
    visible.forEach((h) => (allVisibleSelected ? next.delete(h.symbol) : next.add(h.symbol)));
    setSelected(next);
  };

  return (
    <section className="card">
      <h2>Holdings</h2>

      <div className="chips">
        {Object.entries(STATUS_LABELS).filter(([k]) => counts[k]).map(([k, label]) => (
          <button key={k} className={`chip ${f.status === k ? "on" : ""}`}
                  onClick={() => setF({ ...f, status: f.status === k ? "" : k })}>
            {label} <b>{counts[k]}</b>
          </button>
        ))}
      </div>

      <div className="filters">
        <select value={f.sector} onChange={set("sector")}>
          <option value="">All sectors</option>
          {options.sectors.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={f.thesis} onChange={set("thesis")}>
          <option value="">Any thesis</option>
          <option value="missing">No thesis</option>
          <option value="draft">Draft thesis</option>
          <option value="complete">Complete thesis</option>
        </select>
        {active && <button className="secondary" onClick={() => setF(EMPTY)}>Clear all filters</button>}
        <span className="muted small">
          Column filters: type in the row under the headers. Numbers accept <code>&gt;5</code> <code>&lt;0</code>{" "}
          <code>5..20</code>; % columns are in percent.
        </span>
      </div>

      <p className="muted small">
        Showing {visible.length} of {rows.length} · value {fmt.inr(totalValue)}
        {f.status === "needs_thesis" && " · tick holdings and use “Draft thesis” to unlock signals in bulk"}
      </p>

      {selected.size > 0 && !bulkOpen && (
        <div className="selection-bar">
          <span>{selected.size} selected</span>
          <button onClick={() => setBulkOpen(true)}>Draft thesis for selected</button>
          <button className="secondary" onClick={() => setSelected(new Set())}>Clear selection</button>
        </div>
      )}
      {bulkOpen && (
        <BulkThesis
          symbols={[...selected]}
          onCancel={() => setBulkOpen(false)}
          onDone={(msg) => { setNotice(msg); setBulkOpen(false); setSelected(new Set()); load(); }}
        />
      )}
      {notice && <p className="notice">{notice} <button className="secondary" onClick={() => setNotice(undefined)}>✕</button></p>}

      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th><input type="checkbox" aria-label="Select all shown" checked={allVisibleSelected} onChange={toggleAllVisible} /></th>
              {th("symbol", "Stock")}{th("quantity", "Qty", true)}{th("last_price", "Price", true)}
              {th("day_change_pct", "Day", true)}{th("pnl", "P&L", true)}{th("pnl_pct", "P&L %", true)}
              {th("weight", "Weight", true)}<th>Trend</th>{th("signal", "Signal")}<th>Why</th>
            </tr>
            <tr className="filter-row">
              <th />
              <th><input className="col-filter" value={f.q} onChange={set("q")} placeholder="Symbol / sector"
                         aria-label="Filter symbol or sector" /></th>
              {numFilter("quantity")}{numFilter("last_price")}{numFilter("day_change_pct")}
              {numFilter("pnl")}{numFilter("pnl_pct")}{numFilter("weight")}
              <th>
                <select className="col-filter" value={f.trend} onChange={set("trend")}>
                  <option value="">All</option>
                  {options.trends.map((s) => <option key={s}>{s}</option>)}
                  <option value="NONE">No data</option>
                </select>
              </th>
              <th>
                <select className="col-filter" value={f.signal} onChange={set("signal")}>
                  <option value="">All</option>
                  {options.signals.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
              </th>
              <th><input className="col-filter" value={f.reason} onChange={set("reason")} placeholder="Contains…"
                         aria-label="Filter reason" /></th>
            </tr>
          </thead>
          <tbody>
            {visible.map((h) => (
              <tr key={h.symbol} className={selected.has(h.symbol) ? "selected" : ""}>
                <td><input type="checkbox" aria-label={`Select ${h.symbol}`} checked={selected.has(h.symbol)} onChange={() => toggle(h.symbol)} /></td>
                <td>
                  <a href={`#/stock/${h.symbol}`}>{h.symbol}</a>
                  {h.thesis?.draft && <span className="tag">draft thesis</span>}
                  <div className="muted small">{h.sector}{h.thesis?.horizon ? ` · ${h.thesis.horizon}` : ""}</div>
                </td>
                <td className="num">{h.quantity}</td>
                <td className="num">{fmt.num(h.last_price)}</td>
                <td className={`num ${fmt.sign(h.day_change_pct)}`}>{fmt.pct(h.day_change_pct)}</td>
                <td className={`num ${fmt.sign(h.pnl)}`}>{fmt.inr(h.pnl)}</td>
                <td className={`num ${fmt.sign(h.pnl_pct)}`}>{fmt.pct(h.pnl_pct)}</td>
                <td className="num">{fmt.pct(h.weight)}</td>
                <td>{h.trend ?? "—"}</td>
                <td><Signal state={h.signal} dontAdd={h.dont_add} /></td>
                <td className="small reason" title={`Rule detail: ${h.reason}`}>{h.summary || h.reason}</td>
              </tr>
            ))}
            {visible.length === 0 && (
              <tr><td colSpan={11} className="muted">No holdings match these filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
