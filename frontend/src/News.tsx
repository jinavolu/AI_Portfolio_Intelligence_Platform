import { useEffect, useState } from "react";
import { api, fmt, safeUrl } from "./api";

type Source = { item_id: string; publisher: string; url: string | null; title: string; published: string; official: boolean };
type Cluster = {
  cluster_id: string; event_type: string; official: boolean; sentiment: number; materiality: string; confirmation: string;
  published: string; age_days: number; weight: number; effective_weight: number; summary: string; evidence: string;
  sources: Source[]; duplicates: number; price_since?: number | null;
};
type Recent = { item_id: string; title: string; publisher: string; url: string | null; published: string | null; official: boolean;
  verification: string; rating: { sentiment: number; event_type: string; materiality: string } | null };
export type NewsView = {
  status: string; detail: string; fetched_at: string; score: number | null; confidence: string | null;
  clusters: Cluster[]; excluded: { item_id: string; title: string; why: string }[]; next_results: string | null;
  recent: Recent[]; counts?: { duplicates_merged?: number };
};
export type Shadow = { rule_version: string; state: string; score: number | null; summary: string;
  diagnostics: { impact?: string; news_contribution?: number | null; score_without_news?: number | null } };

const TONE: Record<number, [string, string]> = {
  2: ["Clearly positive", "s-BUY_SIGNAL"], 1: ["Positive", "s-BUY_SIGNAL"], 0: ["Neutral", ""],
  "-1": ["Negative", "s-SELL_SIGNAL"], "-2": ["Clearly negative", "s-SELL_SIGNAL"],
} as any;
const STATUS: Record<string, string> = {
  SUCCESS: "Up to date", PARTIAL: "Partly available", FAILED: "Fetch failed", STALE: "Out of date",
  NOT_COVERED: "No NSE coverage", DISABLED: "Off",
};
const words = (s: string) => s.replace(/_/g, " ").toLowerCase();
const IMPACT: Record<string, string> = {
  NO_IMPACT: "news didn't change the score", REINFORCED: "news reinforced the existing direction",
  MODIFIED: "news changed the score materially", REVERSED: "news reversed the direction", BLOCKED: "a gate stopped it first",
};

/** News for one holding (D11): official disclosures and verified press, grouped into events. */
export function NewsCard({ symbol, news, shadow, liveState, onRefreshed }: {
  symbol: string; news: NewsView | null; shadow: Shadow | null; liveState: string; onRefreshed: () => void;
}) {
  const [msg, setMsg] = useState<string>();
  const refresh = async () => {
    setMsg("Fetching NSE disclosures and press news…");
    try {
      const r = await api<{ status: string }>(`/api/news/refresh/${symbol}`, { method: "POST" });
      setMsg(`Done: ${STATUS[r.status] ?? r.status}.`);
      onRefreshed();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const head = (
    <div className="card-head">
      <h2>News <span className="muted small">last 7 days · official NSE disclosures and verified press</span></h2>
      <button className="secondary" onClick={refresh}>Refresh</button>
    </div>
  );
  if (!news) {
    return <section className="card">{head}<p className="muted">Not fetched yet. News is fetched daily at 07:30 and 18:30, or use Refresh.</p>{msg && <p className="muted small">{msg}</p>}</section>;
  }
  const other = news.recent.filter((r) => !news.clusters.some((c) => c.sources.some((s) => s.item_id === r.item_id)));
  return (
    <section className="card">
      {head}
      <div className="facts">
        <span className="fact"><span className="muted">Status</span> {STATUS[news.status] ?? news.status}</span>
        {news.next_results && <span className="fact"><span className="muted">Next results</span> {news.next_results}</span>}
        <span className="fact"><span className="muted">News score</span> {news.score == null ? "not scored" : news.score.toFixed(2)}</span>
        {news.confidence && <span className="fact"><span className="muted">Confidence</span> {news.confidence.toLowerCase()}</span>}
        <span className="fact"><span className="muted">Fetched</span> {new Date(news.fetched_at).toLocaleString()}</span>
      </div>
      {news.detail && <p className="muted small">{news.detail}</p>}
      {news.score == null && news.clusters.length > 0 && (
        <p className="muted small">Not scored: {news.confidence === "LOW"
          ? "the evidence is thin (a single press source and no official disclosure)."
          : "no qualifying news."}</p>
      )}
      {shadow && (
        <p className="small">
          <span className="muted">News rules being tested ({shadow.rule_version}, shadow):</span>{" "}
          <b>{words(shadow.state)}</b>{shadow.state === liveState ? " (same as the live signal)" : ` (live signal: ${words(liveState)})`}
          {shadow.diagnostics?.impact && <>; {IMPACT[shadow.diagnostics.impact] ?? words(shadow.diagnostics.impact)}</>}
          {shadow.diagnostics?.news_contribution != null && shadow.diagnostics.news_contribution !== 0 &&
            <> ({shadow.diagnostics.news_contribution > 0 ? "+" : ""}{shadow.diagnostics.news_contribution.toFixed(2)} to the score)</>}.
        </p>
      )}
      {news.clusters.length === 0 && <p className="muted">No company news in the last 7 days that counts towards the score.</p>}
      <ul className="alerts">
        {news.clusters.map((c) => {
          const [tone, cls] = TONE[c.sentiment] ?? ["", ""];
          return (
            <li key={c.cluster_id} className="alert">
              <div className="alert-body">
                <div className="alert-head">
                  <span className="alert-title">{c.summary}</span>
                  <span className={`signal ${cls}`}>{tone}</span>
                  <span className="tag">{c.official ? "Official" : "Press"}</span>
                  <span className="chip-sm">{words(c.event_type)} · {c.materiality.toLowerCase()} · {c.confirmation.toLowerCase()}</span>
                  <span className="muted small alert-time">{c.published}</span>
                </div>
                <div className="small muted">“{c.evidence}”</div>
                <div className="small">
                  {c.sources.map((s, i) => (
                    <span key={s.item_id}>{i ? " · " : ""}{safeUrl(s.url) ? <a href={safeUrl(s.url)} target="_blank" rel="noreferrer noopener">{s.publisher}</a> : s.publisher}</span>
                  ))}
                  {c.duplicates > 0 && <span className="muted"> ({c.duplicates} report{c.duplicates > 1 ? "s" : ""} of the same event, counted once)</span>}
                  {c.price_since != null && <span className="muted"> · price since: {fmt.pct(c.price_since)} (coincided with, not necessarily caused by)</span>}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {(other.length > 0 || news.excluded.length > 0) && (
        <details className="small">
          <summary>{other.length} other item{other.length !== 1 ? "s" : ""} not counted</summary>
          <ul>
            {other.map((r) => {
              const why = news.excluded.find((e) => e.item_id === r.item_id)?.why;
              return (
                <li key={r.item_id}>
                  {r.published} · {safeUrl(r.url) ? <a href={safeUrl(r.url)} target="_blank" rel="noreferrer noopener">{r.title}</a> : r.title}
                  <span className="muted"> · {r.official ? "NSE" : r.publisher}{why ? ` · ${why}` : ""}</span>
                </li>
              );
            })}
          </ul>
        </details>
      )}
      {msg && <p className="muted small">{msg}</p>}
    </section>
  );
}

type ShadowRow = {
  symbol: string; weight: number; live: { state: string; score: number | null }; shadow: { state: string | null; score: number | null };
  impact: string | null; news_contribution: number | null; news_status: string | null; news_score: number | null;
  confidence: string | null; events: number;
};

/** Shadow mode (D11.5): live rules-1.3.0 vs the news rules being tested, for every holding. */
export function ShadowComparison() {
  const [d, setD] = useState<{ live_rules: string; shadow_rules: string | null; changed_states: number; rows: ShadowRow[] }>();
  const [err, setErr] = useState<string>();
  const [onlyNews, setOnlyNews] = useState(true);
  useEffect(() => { api<typeof d>("/api/news/shadow").then(setD).catch((e) => setErr(String(e))); }, []);
  if (err) return <section className="card"><h2>News rules in shadow mode</h2><p className="error">{err}</p></section>;
  if (!d) return null;
  if (!d.shadow_rules) {
    return <section className="card"><h2>News rules in shadow mode</h2><p className="muted">News is off, so there is nothing to compare.</p></section>;
  }
  const rows = d.rows.filter((r) => !onlyNews || r.events > 0 || r.live.state !== r.shadow.state);
  return (
    <section className="card">
      <div className="card-head">
        <h2>News rules in shadow mode <span className="muted small">{d.shadow_rules} vs live {d.live_rules}</span></h2>
        <label className="inline"><input type="checkbox" checked={onlyNews} onChange={(e) => setOnlyNews(e.target.checked)} /> Only holdings with news or a difference</label>
      </div>
      <p className="muted small">
        The news rules run alongside the live rules without changing any signal. <b>{d.changed_states}</b> of {d.rows.length} holdings
        would get a different signal. Review these, then decide whether to switch (a recorded decision).
      </p>
      <div className="scroll">
        <table>
          <thead><tr><th>Holding</th><th className="num">Weight</th><th>Live</th><th>With news</th><th>What news did</th><th className="num">News score</th><th>Confidence</th><th className="num">Events</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className={r.live.state !== r.shadow.state ? "warn" : ""}>
                <td><a href={`#/stock/${r.symbol}`}>{r.symbol}</a></td>
                <td className="num">{fmt.pct(r.weight)}</td>
                <td>{words(r.live.state)} <span className="muted small">{r.live.score?.toFixed(2) ?? ""}</span></td>
                <td>{r.shadow.state ? words(r.shadow.state) : "—"} <span className="muted small">{r.shadow.score?.toFixed(2) ?? ""}</span></td>
                <td className="small">{r.impact ? IMPACT[r.impact] ?? words(r.impact) : "—"}
                  {r.news_contribution ? ` (${r.news_contribution > 0 ? "+" : ""}${r.news_contribution.toFixed(2)})` : ""}</td>
                <td className="num">{r.news_score == null ? "—" : r.news_score.toFixed(2)}</td>
                <td className="small">{r.confidence?.toLowerCase() ?? (r.news_status ? STATUS[r.news_status] ?? r.news_status : "not fetched")}</td>
                <td className="num">{r.events}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
