import { useMemo, useState } from "react";
import { api, fmt, useApi } from "../api";

type Mention = { channel: string; date: string | null; url: string; view: "BUY" | "SELL" | null; line: string; from_image: boolean };
type Telegram = { mentions: number; buy: number; sell: number; news: number; latest: Mention; items: Mention[] };
type Row = {
  symbol: string; company: string; industry: string; state: string; score: number | null; summary: string;
  evidence: string; horizon: string | null; signal_source: string; close: number | null;
  day_change_pct: number | null; trend: string | null; rsi14: number | null; pct_from_52w_high: number | null;
  as_of: string | null; held: boolean; telegram: Telegram | null; agreement: "AGREE" | "CONFLICT" | null;
};
type Job = { running: boolean; phase: "prices" | "scan" | null; done: number; total: number; error: string | null; failed: number };
type ScanResponse = {
  rows: Row[]; job: Job; scanned_at?: string; prices_as_of?: string | null; rules?: string;
  rule_set_validated?: boolean; universe?: string;
  telegram_outside?: { symbol: string; name: string | null; held: boolean; telegram: Telegram }[];
  telegram?: { posts: number; reading: number; channels: string[] };
};

const shortDate = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

/** What the Telegram channels said: counts by view, the latest line, and each mention on hover. */
function TelegramCell({ t }: { t: Telegram | null }) {
  if (!t) return <span className="muted">—</span>;
  const tip = t.items.map((m) => `${shortDate(m.date)} · ${m.channel}${m.view ? ` · ${m.view.toLowerCase()}` : ""}: ${m.line}`).join("\n");
  return (
    <div title={tip}>
      {t.buy > 0 && <span className="stock stock-BUY">{t.buy} buy</span>}{" "}
      {t.sell > 0 && <span className="stock stock-SELL">{t.sell} sell</span>}{" "}
      {t.news > 0 && <span className="stock">{t.news} news</span>}
      <div className="small">
        <a href={t.latest.url} target="_blank" rel="noopener noreferrer">{t.latest.line.slice(0, 90)}</a>
        <span className="muted"> · {shortDate(t.latest.date)}</span>
      </div>
    </div>
  );
}

type Group = "buy" | "weak" | "hold" | "all";
const GROUPS: [Group, string][] = [["buy", "Buy signals"], ["weak", "Weak / sell"], ["hold", "Hold"], ["all", "All"]];
const inGroup = (r: Row, g: Group) =>
  g === "all" || (g === "buy" ? r.state.startsWith("BUY") : g === "weak" ? r.state.startsWith("SELL") : r.state === "HOLD");
const HORIZON: Record<string, string> = { SHORT_TERM: "Short", MEDIUM_TERM: "Medium", LONG_TERM: "Long" };
const LABEL = (r: Row) =>
  r.state.startsWith("BUY") ? "Buy signal" : r.state.startsWith("SELL") ? (r.held ? "Sell signal" : "Weak")
    : r.state === "NO_ACTION" ? "No action" : r.state.replace("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());

const daysOld = (iso?: string | null) => (iso ? Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000) : null);

/** The app's own chart rules over the Nifty 500, including stocks you don't hold. Prompts to look, not advice. */
export default function Scanner() {
  // Follows a running scan or price refresh, and Telegram images still being read (the hook keeps
  // polling after a failed reply, so "Scoring: 120 of 500…" never freezes).
  const scan = useApi<ScanResponse>("/api/scanner", {
    poll: (d) => (d?.job.running ? 3000 : d?.telegram?.reading ? 10000 : null),
  });
  const data = scan.data, load = scan.reload;
  const [runErr, setRunErr] = useState<string>();
  const err = runErr ?? scan.error;
  const [group, setGroup] = useState<Group>("buy");
  const [horizon, setHorizon] = useState("");
  const [sector, setSector] = useState("");
  const [q, setQ] = useState("");
  const [notHeld, setNotHeld] = useState(false);
  const [onTelegram, setOnTelegram] = useState(false);

  const run = async (refresh: boolean) => {
    setRunErr(undefined);
    try {
      await api(`/api/scanner/run?refresh_prices=${refresh}`, { method: "POST" });
      load();
    } catch (e) {
      setRunErr((e as Error).message);
    }
  };

  const rows = data?.rows ?? [];
  const sectors = useMemo(() => [...new Set(rows.map((r) => r.industry).filter(Boolean))].sort(), [rows]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows
      .filter((r) => inGroup(r, group) && (!horizon || r.horizon === horizon) && (!sector || r.industry === sector)
        && (!notHeld || !r.held) && (!onTelegram || r.telegram)
        && (!needle || `${r.symbol} ${r.company}`.toLowerCase().includes(needle)))
      // No score (too little history) sorts last either way, never as a 0 in the middle.
      .sort((a, b) => a.score == null || b.score == null ? Number(a.score == null) - Number(b.score == null)
        : group === "weak" ? a.score - b.score : b.score - a.score);
  }, [rows, group, horizon, sector, notHeld, onTelegram, q]);
  const onTelegramCount = rows.filter((r) => r.telegram).length;
  const count = (g: Group) => rows.filter((r) => inGroup(r, g)).length;

  const job = data?.job;
  const age = daysOld(data?.prices_as_of);
  const stale = age != null && age > 3;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <h2>Scanner · {data?.universe ? data.universe.replace("nifty", "Nifty ") : "Nifty 500"}</h2>
          <span className="chip-sm">{data?.rules ?? "rules"} · {data?.rule_set_validated ? "back-tested" : "not back-tested"}</span>
        </div>
        <p className="muted small">
          The same chart rules your holdings use (trend, 200-day average, momentum, heavy-volume breakouts), run on
          every stock in the index, including ones you don't own. The horizon comes from each chart's volatility.
          {" "}<b>These rules haven't passed back-testing yet</b>, so a buy signal is a prompt to look closer, not a
          recommendation. For your own use only: sharing picks falls under SEBI's research analyst rules.
        </p>
        <div className="filters">
          <span className={stale ? "warn" : "muted"}>
            Prices up to {data?.prices_as_of ?? "—"}
            {stale ? ": out of date, so most stocks show no signal. Refresh them (Kite login needed)." : ""}
          </span>
          <button disabled={job?.running} onClick={() => run(true)}>Refresh prices from Kite</button>
          <button className="secondary" disabled={job?.running} onClick={() => run(false)}>Rescan</button>
        </div>
        {job?.running && (
          <div className="small">
            {job.phase === "prices" ? "Downloading the latest prices" : "Scoring"}: {job.done} of {job.total}
            {job.phase === "prices" ? " (about 3–4 minutes)" : ""}…
          </div>
        )}
        {job && !job.running && job.failed > 0 && <div className="warn small">{job.failed} stock(s) couldn't be refreshed.</div>}
        {job?.error && <div className="error">{job.error}</div>}
        {err && <div className="error">{err}</div>}
      </div>

      <div className="card">
        <div className="filters">
          <div className="seg">
            {GROUPS.map(([g, label]) => (
              <button key={g} className={group === g ? "on" : ""} onClick={() => setGroup(g)}>{label} ({count(g)})</button>
            ))}
          </div>
          <select value={horizon} onChange={(e) => setHorizon(e.target.value)}>
            <option value="">Any horizon</option>
            {Object.entries(HORIZON).map(([k, v]) => <option key={k} value={k}>{v} term</option>)}
          </select>
          <select value={sector} onChange={(e) => setSector(e.target.value)}>
            <option value="">All sectors</option>
            {sectors.map((s) => <option key={s}>{s}</option>)}
          </select>
          <input placeholder="Search stock" aria-label="Search stock" value={q} onChange={(e) => setQ(e.target.value)} />
          <label className="inline">
            <input type="checkbox" checked={notHeld} onChange={(e) => setNotHeld(e.target.checked)} /> Only stocks I don't hold
          </label>
          <label className="inline">
            <input type="checkbox" checked={onTelegram} onChange={(e) => setOnTelegram(e.target.checked)} /> Only stocks
            on Telegram ({onTelegramCount})
          </label>
        </div>
        {!data && <p className="muted">Loading…</p>}
        {data && rows.length === 0 && !job?.running && <p className="muted">No scan yet.</p>}
        {shown.length > 0 && (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Stock</th><th>Signal</th><th className="num">Score</th><th>Why (chart)</th><th>Telegram</th>
                  <th className="num">Close</th><th className="num">Day</th><th className="num">RSI</th>
                  <th className="num">From 52w high</th><th>Horizon</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.symbol}>
                    <td>
                      {r.held ? <a href={`#/stock/${r.symbol}`}><b>{r.symbol}</b></a> : <b>{r.symbol}</b>}
                      {r.held && <span className="tag">you hold</span>}
                      <div className="muted small">{r.company}{r.industry ? ` · ${r.industry}` : ""}</div>
                    </td>
                    <td>
                      <span className={`signal s-${r.state}`}>{LABEL(r)}</span>
                      {r.agreement === "AGREE" && <div className="small pos">Telegram agrees</div>}
                      {r.agreement === "CONFLICT" && <div className="small neg">Telegram disagrees</div>}
                    </td>
                    <td className="num">{r.score == null ? "—" : r.score.toFixed(2)}</td>
                    <td className="small" title={r.evidence}>{r.summary}</td>
                    <td className="tg-cell"><TelegramCell t={r.telegram} /></td>
                    <td className="num">{fmt.inr(r.close)}</td>
                    <td className={`num ${fmt.sign(r.day_change_pct)}`}>{fmt.pct(r.day_change_pct)}</td>
                    <td className="num">{r.rsi14 == null ? "—" : r.rsi14.toFixed(0)}</td>
                    <td className="num">{fmt.pct(r.pct_from_52w_high)}</td>
                    <td>{r.horizon ? HORIZON[r.horizon] : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && rows.length > 0 && shown.length === 0 && <p className="muted">No stocks match these filters.</p>}
        <p className="muted small">
          Telegram: {data?.telegram?.channels?.length
            ? `the last ${data.telegram.posts} posts of ${data.telegram.channels.join(", ")}`
              + (data.telegram.reading ? ` (reading ${data.telegram.reading} more images…)` : "")
              + ". Green/red = the post's own buy/sell wording, grey = news. Shown beside the chart signal; it doesn't change it."
            : "no channels yet. Add one on the Feed page."}
        </p>
        <p className="muted small">
          Hover a reason to see the numbers behind it. Score runs from −1 to +1: 0.6 or more is the buy band (and needs
          volume to confirm it), −0.6 or less the sell band. Scanned {data?.scanned_at ? new Date(data.scanned_at).toLocaleString("en-IN") : "—"}.
        </p>
      </div>

      {(data?.telegram_outside?.length ?? 0) > 0 && (
        <div className="card">
          <h3>On Telegram, outside the Nifty 500</h3>
          <p className="muted small">Named in the channels but not in the stored index, so the app has no chart signal for them.</p>
          <div className="scroll">
            <table>
              <thead><tr><th>Stock</th><th>Telegram</th></tr></thead>
              <tbody>
                {data!.telegram_outside!.map((o) => (
                  <tr key={o.symbol}>
                    <td>
                      {o.held ? <a href={`#/stock/${o.symbol}`}><b>{o.symbol}</b></a> : <b>{o.symbol}</b>}
                      {o.held && <span className="tag">you hold</span>}
                      <div className="muted small">{o.name}</div>
                    </td>
                    <td className="tg-cell"><TelegramCell t={o.telegram} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
