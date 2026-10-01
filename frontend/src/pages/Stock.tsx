import { useEffect, useState } from "react";
import { api, fmt } from "../api";
import { Signal } from "./Dashboard";
import { ValueInput } from "../ValueInput";
import { WarningEditor } from "../Warnings";
import { Markdown } from "../Markdown";
import { NewsCard, type NewsView, type Shadow } from "../News";

type Candle = { date: string; close: number };
type Gate = { gate: number; name: string; status: string; detail: string };
type Category = "THESIS" | "BUSINESS" | "TECHNICAL";
type Condition = {
  id?: string;
  category: Category;
  metric: string;
  op: string;
  value: number | string;
  description: string;
  origin?: "OWNER" | "SUGGESTED";
  status?: "ACTIVE" | "PROPOSED" | "DISMISSED";
  confirmed_at?: string | null;
};
type ConditionResult = {
  id: string; category: Category; description: string; metric: string; op: string; value: number | string;
  result: "MET" | "NOT_MET" | "CANNOT_CHECK"; actual: number | string | null; reason: string;
  source: string; source_as_of: string | null; default: boolean;
  period?: string | null; inputs?: Evidence | null; fetched_at?: string | null;
};
type Evidence = { label: string; unit: string; latest: { period_end: string; value: number | null }; year_ago: { period_end: string; value: number | null } };
type Fundamental = {
  status: "OK" | "NOT_FOUND" | "ERROR"; detail?: string; fetched_at: string;
  provider?: string; provider_symbol?: string; basis?: string; financial?: boolean; latest_quarter?: string | null;
  quarters?: { period_end: string; total_income: number | null; pbt: number | null; net_profit: number | null }[];
  values?: Record<string, number | null>; reasons?: Record<string, string>; issues?: string[];
};
type Metric = { description: string; categories: Category[]; source: string; kind: "enum" | "fraction" | "number"; values: string[] | null };
type Catalogue = Record<string, Metric>;

const CATEGORY_LABEL: Record<Category, string> = {
  THESIS: "Thesis", BUSINESS: "Business", TECHNICAL: "Technical warning",
};
const OPS = ["<", "<=", ">", ">=", "==", "!="];
type Thesis = {
  why_bought: string;
  horizon: string | null;
  assumptions: string[];
  metrics_to_monitor: string[];
  invalidation_conditions: Condition[];
  notes: string;
  version?: number;
  draft?: boolean;
};
type Detail = {
  holding: {
    symbol: string;
    data_as_of: Record<string, string | null>;
    portfolio: Record<string, any>;
    technical: Record<string, any> | null;
    thesis: Thesis | null;
    risk: Record<string, any> | null;
    gates: Gate[];
    component_scores: Record<string, number | null>;
    decision: {
      state: string; reason: string; summary?: string; rule_version?: string; score: number | null; dont_add: boolean; tax_impact: any;
      guidance?: { evidence?: string; basis?: string; action?: string; note?: string };
    };
    conditions: ConditionResult[] | null;
    fundamental: Fundamental | null;
    news: NewsView | null;
    shadow: Shadow | null;
  };
  candles: Candle[];
};
type Explanation = { text: string; model: string; cached: boolean; grounded: boolean; ungrounded_numbers: string[]; flag?: string };

const INDICATORS = ["close", "ema20", "ema50", "sma200", "rsi14", "macd_hist", "volume_ratio", "high_52w", "low_52w",
  "pct_from_52w_high", "trend", "long_term_trend", "support", "resistance", "breakout", "breakdown"];

export default function Stock({ symbol }: { symbol: string }) {
  const [d, setD] = useState<Detail>();
  const [err, setErr] = useState<string>();
  const [exp, setExp] = useState<Explanation>();
  const [expErr, setExpErr] = useState<string>();
  const [catalogue, setCatalogue] = useState<Catalogue>();
  const load = () => api<Detail>(`/api/holdings/${symbol}`).then(setD).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps -- remounted per symbol (App.tsx)
  useEffect(() => { api<Catalogue>("/api/theses/metrics").then(setCatalogue).catch((e) => setErr(String(e))); }, []);

  if (err) return <p className="error">{err}</p>;
  if (!d || !catalogue) return <p>Loading…</p>;
  const h = d.holding;
  const t = h.technical;

  return (
    <>
      <h1>{h.symbol} <Signal state={h.decision.state} dontAdd={h.decision.dont_add} /></h1>
      <p className="summary"><b>{h.decision.summary || h.decision.reason}</b></p>
      {h.decision.guidance?.evidence && (
        <div className="card">
          <p><span className="muted">What the app sees:</span> {h.decision.guidance.evidence}</p>
          {h.decision.guidance.basis && <p><span className="muted">What it's based on:</span> {h.decision.guidance.basis}</p>}
          {h.decision.guidance.action && <p><span className="muted">What to do:</span> {h.decision.guidance.action}</p>}
          {h.decision.guidance.note && <p className="warn small">{h.decision.guidance.note}</p>}
        </div>
      )}
      <p className="muted small">Rule detail: {h.decision.reason}{h.decision.score != null && <> · score {h.decision.score}</>} · {h.decision.rule_version ?? ""}</p>

      <section className="card"><Chart candles={d.candles} /></section>

      <div className="grid2">
        <section className="card">
          <h2>Position</h2>
          <dl>
            <dt>Quantity</dt><dd>{h.portfolio.quantity}</dd>
            <dt>Average / Last</dt><dd>{fmt.inr(h.portfolio.average_price)} / {fmt.inr(h.portfolio.last_price)}</dd>
            <dt>P&L</dt><dd className={fmt.sign(h.portfolio.pnl)}>{fmt.inr(h.portfolio.pnl)} ({fmt.pct(h.portfolio.pnl_pct)})</dd>
            <dt>Weight</dt><dd>{fmt.pct(h.portfolio.weight)} (sector {fmt.pct(h.risk?.sector_weight)})</dd>
            <dt>XIRR</dt><dd>{fmt.pct(h.portfolio.xirr)}</dd>
            <dt>Short / long-term gain</dt><dd>{fmt.inr(h.portfolio.short_term_gain)} / {fmt.inr(h.portfolio.long_term_gain)}</dd>
          </dl>
          <h3>Lots</h3>
          <table>
            <thead><tr><th>Bought</th><th>Qty</th><th>Price</th><th>Term</th><th>To LT</th></tr></thead>
            <tbody>
              {h.portfolio.lots.map((l: any, i: number) => (
                <tr key={i}>
                  <td>{l.buy_date ?? "unknown"}</td><td className="num">{l.quantity}</td>
                  <td className="num">{fmt.num(l.buy_price)}</td><td>{l.tax_term}</td>
                  <td className="num">{l.days_to_long_term != null ? `${l.days_to_long_term} d` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {h.decision.tax_impact && (
            <p className="warn">Estimated tax if sold now: {fmt.inr(h.decision.tax_impact.estimated_tax)}. {h.decision.tax_impact.note}</p>
          )}
        </section>

        <section className="card">
          <h2>Technicals <span className="muted small">as of {h.data_as_of.candles}</span></h2>
          {!t && <p className="muted">No candle data.</p>}
          {t && (
            <dl>
              {INDICATORS.map((k) => (
                <span key={k} style={{ display: "contents" }}>
                  <dt>{k}</dt><dd>{t[k] == null ? "—" : String(t[k])}</dd>
                </span>
              ))}
            </dl>
          )}
        </section>
      </div>

      <div className="grid2">
        <section className="card">
          <h2>Gates</h2>
          <table>
            <tbody>
              {h.gates.map((g) => (
                <tr key={g.gate}>
                  <td>{g.gate}</td><td>{g.name}</td><td className={`gate-${g.status}`}>{g.status}</td>
                  <td className="muted small">{g.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Component scores</h3>
          <p className="muted">
            {Object.entries(h.component_scores).map(([k, v]) => `${k}: ${v ?? "n/a"}`).join(" · ") || "Not scored (a gate stopped evaluation)"}
          </p>
        </section>

        <section className="card">
          <h2>Explanation</h2>
          <button onClick={() => { setExpErr(undefined); api<Explanation>(`/api/holdings/${symbol}/explain`, { method: "POST" }).then(setExp).catch((e) => setExpErr(String(e))); }}>
            Explain this holding
          </button>
          {expErr && <p className="error">{expErr}</p>}
          {exp && (
            <>
              <Markdown text={exp.text} />
              <p className="muted small">
                {exp.model} · {exp.cached ? "cached" : "fresh"} · {exp.grounded ? "grounded ✓" : `UNGROUNDED: ${exp.ungrounded_numbers.join(", ")}`}
              </p>
              {exp.flag && <p className="warn">{exp.flag}</p>}
            </>
          )}
        </section>
      </div>

      <Fundamentals symbol={symbol} f={h.fundamental} catalogue={catalogue} onRefreshed={load} />
      <NewsCard symbol={symbol} news={h.news} shadow={h.shadow} liveState={h.decision.state} onRefreshed={load} />

      <section className="card">
        <h2>Default technical warnings</h2>
        <WarningEditor symbol={symbol} results={h.conditions?.filter((c) => c.default)} onSaved={load} />
      </section>
      <ThesisSummary symbol={symbol} thesis={h.thesis} results={h.conditions?.filter((c) => !c.default) ?? null} catalogue={catalogue} onSaved={load} />
      <ThesisEditor key={`${symbol}-${h.thesis?.version ?? 0}`} symbol={symbol} thesis={h.thesis} catalogue={catalogue} onSaved={load} />
    </>
  );
}

function Chart({ candles }: { candles: Candle[] }) {
  if (candles.length < 2) return <p className="muted">No chart data.</p>;
  const W = 900, H = 220, P = 30;
  const closes = candles.map((c) => c.close);
  const min = Math.min(...closes), max = Math.max(...closes);
  const x = (i: number) => P + (i / (candles.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - min) / (max - min || 1)) * (H - 2 * P);
  const path = closes.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Closing price, last year">
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" />
      <text x={P} y={14} className="axis">{fmt.num(max)}</text>
      <text x={P} y={H - 8} className="axis">{fmt.num(min)} · {candles[0].date} → {candles[candles.length - 1].date}</text>
    </svg>
  );
}

function thesisBody(thesis: Thesis) {
  const { version: _v, symbol: _s, updated_at: _u, ...body } = thesis as Thesis & { symbol?: string; updated_at?: string };
  return body;
}

function showValue(v: number | string | null, metric: string, catalogue: Catalogue) {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  return catalogue[metric]?.kind === "fraction" ? fmt.pct(v) : fmt.num(v);
}

const crore = (v: number | null | undefined) => (v == null ? "—" : `₹${fmt.num(Math.round(v))} cr`);

const BUSINESS_LABELS: Record<string, string> = {
  revenue_yoy: "Revenue growth (YoY)", profit_yoy: "Net profit growth (YoY)", pbt_yoy: "Pre-tax profit growth (YoY)",
  net_profit_q: "Net profit, latest quarter", loss_quarters_4: "Loss-making quarters (last 4)", pb_ratio: "Price / book",
  debt_to_equity: "Debt / equity", promoter_holding: "Promoter holding",
};

/** Reported figures from the fundamentals provider: standalone, with the quarter end shown first (D7.7, D8). */
function Fundamentals({ symbol, f, catalogue, onRefreshed }: { symbol: string; f: Fundamental | null; catalogue: Catalogue; onRefreshed: () => void }) {
  const [msg, setMsg] = useState<string>();
  const refresh = async () => {
    setMsg("Fetching…");
    try {
      const r = await api<{ status: string }>(`/api/fundamentals/refresh/${symbol}`, { method: "POST" });
      setMsg(r.status === "OK" ? "Updated." : `Provider: ${r.status}`);
      onRefreshed();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const head = (
    <div className="card-head">
      <h2>Fundamentals{f?.status === "OK" && <span className="muted small"> {f.basis?.toLowerCase()} · {f.provider}</span>}</h2>
      <button className="secondary" onClick={refresh}>Refresh</button>
    </div>
  );
  if (!f || f.status !== "OK") {
    return (
      <section className="card">
        {head}
        <p className="muted">
          {!f ? "Not fetched yet. They are fetched daily before market open, or use Refresh."
            : f.status === "NOT_FOUND" ? `The provider doesn't cover this stock (${f.detail}). Business conditions show "Cannot check".`
            : `The last fetch failed: ${f.detail}`}
        </p>
        {msg && <p className="muted small">{msg}</p>}
      </section>
    );
  }
  const values = f.values ?? {};
  return (
    <section className="card">
      {head}
      <p><b>Latest quarter: {f.latest_quarter}</b> <span className="muted small">· fetched {new Date(f.fetched_at).toLocaleString()} · standalone figures of the listed company, not the whole group</span></p>
      <div className="grid2">
        <table>
          <thead><tr><th>Quarter</th><th className="num">Total income</th><th className="num">PBT</th><th className="num">Net profit</th></tr></thead>
          <tbody>
            {(f.quarters ?? []).map((q) => (
              <tr key={q.period_end}>
                <td>{q.period_end}</td><td className="num">{crore(q.total_income)}</td>
                <td className="num">{crore(q.pbt)}</td><td className={`num ${q.net_profit != null && q.net_profit < 0 ? "neg" : ""}`}>{crore(q.net_profit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl>
          {Object.keys(BUSINESS_LABELS).map((m) => (
            <span key={m} style={{ display: "contents" }}>
              <dt>{BUSINESS_LABELS[m]}</dt>
              <dd>
                {values[m] == null
                  ? <span className="muted small">Cannot check: {f.reasons?.[m] ?? "not available"}</span>
                  : m === "net_profit_q" ? crore(values[m]) : showValue(values[m], m, catalogue)}
              </dd>
            </span>
          ))}
        </dl>
      </div>
      {!!f.issues?.length && (
        <details className="small">
          <summary className="muted">{f.issues.length} value{f.issues.length > 1 ? "s" : ""} not trusted from the provider</summary>
          <ul>{f.issues.map((i) => <li key={i}>{i}</li>)}</ul>
        </details>
      )}
      {msg && <p className="muted small">{msg}</p>}
    </section>
  );
}

const RESULT_BADGE = {
  MET: <span className="signal s-REVIEW">Met</span>,
  NOT_MET: <span className="signal s-HOLD">Not met</span>,
  CANNOT_CHECK: <span className="signal">Cannot check</span>,
};

function ResultList({ items, catalogue }: { items: ConditionResult[]; catalogue: Catalogue }) {
  return (
    <ul className="checks">
      {items.map((r) => (
        <li key={r.id}>
          {RESULT_BADGE[r.result]} {r.description}{" "}
          <span className="muted small">
            ({r.metric} {r.op} {showValue(r.value, r.metric, catalogue)}
            {r.result === "CANNOT_CHECK" ? `: ${r.reason}` : `; now ${showValue(r.actual, r.metric, catalogue)}`}
            {r.period ? ` · ${r.period}` : r.source_as_of && ` · ${r.source} as of ${r.source_as_of}`})
          </span>
          {r.inputs && r.result !== "CANNOT_CHECK" && (
            <div className="muted small">
              {r.inputs.label}: {crore(r.inputs.latest.value)} ({r.inputs.latest.period_end}) vs {crore(r.inputs.year_ago.value)} ({r.inputs.year_ago.period_end})
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

/** What the decision engine currently uses, and whether the owner has confirmed it. */
function ThesisSummary({ symbol, thesis, results, catalogue, onSaved }: {
  symbol: string; thesis: Thesis | null; results: ConditionResult[] | null; catalogue: Catalogue; onSaved: () => void;
}) {
  const [msg, setMsg] = useState<string>();
  if (!thesis) {
    return (
      <section className="card">
        <h2>Thesis <span className="signal s-REVIEW">Missing</span></h2>
        <p className="muted">No thesis recorded, so this holding gets no signal. Fill in the form below.</p>
      </section>
    );
  }
  const put = async (body: object, done: (v: number) => string) => {
    try {
      const saved = await api<Thesis>(`/api/theses/${symbol}`, { method: "PUT", body: JSON.stringify(body) });
      setMsg(done(saved.version!));
      onSaved();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const confirm = () => put({ ...thesisBody(thesis), draft: false }, (v) => `Confirmed as your thesis (version ${v}).`);
  // Accepting or dismissing a suggestion changes only that condition; the thesis keeps its draft state.
  const decide = (id: string, status: "ACTIVE" | "DISMISSED") => put(
    { ...thesisBody(thesis), invalidation_conditions: thesis.invalidation_conditions.map((c) => (c.id === id ? { ...c, status } : c)) },
    (v) => `${status === "ACTIVE" ? "Accepted" : "Dismissed"} (version ${v}).`,
  );
  const active = results ?? [];
  const review = active.filter((r) => r.category !== "TECHNICAL");
  const technical = active.filter((r) => r.category === "TECHNICAL");
  const proposed = thesis.invalidation_conditions.filter((c) => c.status === "PROPOSED");
  const dismissed = thesis.invalidation_conditions.filter((c) => c.status === "DISMISSED").length;
  const list = (xs: string[]) => (xs.length ? <ul>{xs.map((x) => <li key={x}>{x}</li>)}</ul> : <span className="muted">none recorded</span>);
  return (
    <section className="card">
      <div className="card-head">
        <h2>
          Thesis v{thesis.version}{" "}
          {thesis.draft
            ? <span className="signal s-REVIEW">Draft: not confirmed by you</span>
            : <span className="signal s-HOLD">Confirmed by you</span>}
        </h2>
        {thesis.draft && <button onClick={confirm}>Confirm as my thesis</button>}
      </div>
      {thesis.draft && (
        <p className="warn small">
          This horizon and reason were assigned from the stock's profile, not from your stated reasons. Confirm it as is, or
          edit it below and save; either makes it your own thesis.
        </p>
      )}
      <dl>
        <dt>Horizon</dt><dd>{thesis.horizon ?? <span className="warn">none</span>}</dd>
        <dt>Reason for holding</dt><dd>{thesis.why_bought}</dd>
        <dt>Assumptions</dt><dd>{list(thesis.assumptions)}</dd>
        <dt>Metrics to monitor</dt><dd>{list(thesis.metrics_to_monitor)}</dd>
        <dt>Invalidation conditions <span className="muted small">(thesis and business: can trigger a review)</span></dt>
        <dd>
          {results == null && thesis.invalidation_conditions.some((c) => c.status === "ACTIVE")
            ? <span className="muted">results appear after the next refresh</span>
            : review.length
            ? <ResultList items={review} catalogue={catalogue} />
            : <span className="muted">none active: the "thesis broken → review" check cannot fire for this holding</span>}
        </dd>
        <dt>Your technical warnings <span className="muted small">(alert only: never a review or a sale)</span></dt>
        <dd>{technical.length ? <ResultList items={technical} catalogue={catalogue} /> : <span className="muted">none of your own; the defaults are above</span>}</dd>
        {thesis.notes && <><dt>Notes</dt><dd className="small">{thesis.notes}</dd></>}
      </dl>
      {proposed.length > 0 && (
        <>
          <h3>Suggestions awaiting your decision</h3>
          <p className="muted small">Suggested, not yours yet: none of these is checked until you accept it.</p>
          <ul className="checks">
            {proposed.map((c) => (
              <li key={c.id}>
                <span className="tag">{CATEGORY_LABEL[c.category]}</span> {c.description || `${c.metric} ${c.op} ${c.value}`}{" "}
                <span className="muted small">({c.metric} {c.op} {showValue(c.value, c.metric, catalogue)})</span>{" "}
                <button onClick={() => decide(c.id!, "ACTIVE")}>Accept</button>{" "}
                <button onClick={() => decide(c.id!, "DISMISSED")}>Dismiss</button>
              </li>
            ))}
          </ul>
        </>
      )}
      {dismissed > 0 && <p className="muted small">{dismissed} dismissed suggestion{dismissed > 1 ? "s" : ""} (kept in the thesis history).</p>}
      {msg && <p className="notice">{msg}</p>}
    </section>
  );
}

function ConditionRow({ c, catalogue, onChange, onRemove }: {
  c: Condition; catalogue: Catalogue; onChange: (c: Condition) => void; onRemove: () => void;
}) {
  const allowed = (cat: Category) => Object.keys(catalogue).filter((m) => catalogue[m].categories.includes(cat));
  const initial = (m: string) => (catalogue[m].kind === "enum" ? catalogue[m].values![0] : 0);
  const setCategory = (category: Category) => {
    const metric = allowed(category).includes(c.metric) ? c.metric : allowed(category)[0];
    onChange({ ...c, category, metric, value: metric === c.metric ? c.value : initial(metric) });
  };
  const suggested = c.origin === "SUGGESTED";
  return (
    <div className="filters">
      <select value={c.category} onChange={(e) => setCategory(e.target.value as Category)}>
        {(Object.keys(CATEGORY_LABEL) as Category[]).map((cat) => (
          <option key={cat} value={cat} disabled={!allowed(cat).length}>
            {CATEGORY_LABEL[cat]}{allowed(cat).length ? "" : " (after fundamentals are validated)"}
          </option>
        ))}
      </select>
      <select value={c.metric} onChange={(e) => onChange({ ...c, metric: e.target.value, value: initial(e.target.value) })}>
        {allowed(c.category).map((m) => <option key={m} value={m} title={catalogue[m].description}>{m}</option>)}
      </select>
      <select value={c.op} onChange={(e) => onChange({ ...c, op: e.target.value })} style={{ minWidth: 60 }}>
        {OPS.map((o) => <option key={o}>{o}</option>)}
      </select>
      <ValueInput key={c.metric} metric={catalogue[c.metric]} value={c.value} onChange={(value) => onChange({ ...c, value })} />
      <input placeholder="In your words, e.g. “Profit falling means my reason is gone”" aria-label="Condition in your words"
        value={c.description}
        onChange={(e) => onChange({ ...c, description: e.target.value })} />
      {suggested && (
        <select value={c.status} onChange={(e) => onChange({ ...c, status: e.target.value as Condition["status"] })}>
          <option value="PROPOSED">Suggested: not checked</option>
          <option value="ACTIVE">Accepted</option>
          <option value="DISMISSED">Dismissed</option>
        </select>
      )}
      <button onClick={onRemove}>Remove</button>
    </div>
  );
}

function ThesisEditor({ symbol, thesis, catalogue, onSaved }: { symbol: string; thesis: Thesis | null; catalogue: Catalogue; onSaved: () => void }) {
  const [form, setForm] = useState<Thesis>(
    thesis ?? { why_bought: "", horizon: null, assumptions: [], metrics_to_monitor: [], invalidation_conditions: [], notes: "" },
  );
  const [msg, setMsg] = useState<string>();
  // The one-per-line lists are edited as raw text and split on save: splitting on every keystroke
  // trims the space you just typed and drops the empty line Enter just made.
  const [assumptions, setAssumptions] = useState(form.assumptions.join("\n"));
  const [metrics, setMetrics] = useState(form.metrics_to_monitor.join("\n"));
  // A row's React key: its id once saved, else a client-side one, so removing a row doesn't hand
  // its typed value to the next row (ValueInput keeps its own text).
  const [rowKeys, setRowKeys] = useState<string[]>(() => form.invalidation_conditions.map((c, i) => c.id ?? `new-${i}`));
  const conds = form.invalidation_conditions;
  const setConds = (next: Condition[]) => setForm({ ...form, invalidation_conditions: next });
  const add = (category: Category) => {
    const metric = Object.keys(catalogue).find((m) => catalogue[m].categories.includes(category))!;
    const value = catalogue[metric].kind === "enum" ? catalogue[metric].values![0] : 0;
    setConds([...conds, { category, metric, op: "<", value, description: "", origin: "OWNER", status: "ACTIVE" }]);
    setRowKeys([...rowKeys, `new-${Date.now()}-${rowKeys.length}`]);
  };
  const remove = (i: number) => {
    setConds(conds.filter((_, j) => j !== i));
    setRowKeys(rowKeys.filter((_, j) => j !== i));
  };

  const save = async () => {
    try {
      // Saving here is a deliberate, per-holding thesis, so it is no longer a draft.
      const body = { ...form, assumptions: lines(assumptions), metrics_to_monitor: lines(metrics), draft: false };
      const saved = await api<Thesis>(`/api/theses/${symbol}`, { method: "PUT", body: JSON.stringify(body) });
      setMsg(`Saved as version ${saved.version}`);
      onSaved();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const lines = (v: string) => v.split("\n").map((s) => s.trim()).filter(Boolean);

  return (
    <section className="card">
      <h2>{thesis ? "Edit thesis" : "Record a thesis"} <span className="muted small">saving makes it your confirmed thesis</span></h2>
      <label>Why did I buy?<textarea value={form.why_bought} onChange={(e) => setForm({ ...form, why_bought: e.target.value })} /></label>
      <label>Horizon
        <select value={form.horizon ?? ""} onChange={(e) => setForm({ ...form, horizon: e.target.value || null })}>
          <option value="">— choose —</option>
          <option>SHORT_TERM</option><option>MEDIUM_TERM</option><option>LONG_TERM</option>
        </select>
      </label>
      <label>Key assumptions (one per line)
        <textarea value={assumptions} onChange={(e) => setAssumptions(e.target.value)} />
      </label>
      <label>Metrics to monitor (one per line)
        <textarea value={metrics} onChange={(e) => setMetrics(e.target.value)} />
      </label>
      <h3>Conditions</h3>
      <p className="muted small">
        <b>Thesis</b> and <b>business</b> conditions say what would prove your reason wrong; when one is met the holding goes to
        review. <b>Technical warnings</b> only raise an alert and never cause a review or a sale.
      </p>
      {conds.map((c, i) => (
        <ConditionRow key={rowKeys[i]} c={c} catalogue={catalogue}
          onChange={(next) => setConds(conds.map((x, j) => (j === i ? next : x)))}
          onRemove={() => remove(i)} />
      ))}
      <p>
        <button onClick={() => add("THESIS")}>+ Thesis condition</button>{" "}
        <button onClick={() => add("TECHNICAL")}>+ Technical warning</button>
      </p>
      <button onClick={save}>Save thesis</button> {msg && <span className="muted">{msg}</span>}
    </section>
  );
}
