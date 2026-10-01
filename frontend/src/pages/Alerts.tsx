import { useMemo, useState } from "react";
import { api, safeUrl, useApi } from "../api";
import { WarningEditor } from "../Warnings";

export type Alert = {
  id: number;
  created_at: string;
  symbol: string;
  type: string;
  severity: "high" | "medium" | "info";
  message: string;
  acknowledged_at: string | null;
  details?: {
    category?: string; horizon?: string | null; horizon_draft?: boolean; url?: string | null;
    explain?: { what: string; meaning: string; action: string; headline?: string; facts?: [string, string][] };
  };
};

const HORIZON_TAG: Record<string, string> = { SHORT_TERM: "Short term", MEDIUM_TERM: "Medium term", LONG_TERM: "Long term" };

const CATEGORY_CHIP: Record<string, string> = {
  THESIS: "Your thesis", BUSINESS: "Business", TECHNICAL: "Price signal, not your thesis", NEWS: "News",
};

/** For alerts raised before explanations were stored: what the type means in general. */
const TYPE_HELP: Record<string, string> = {
  RESISTANCE_CROSSED: "Resistance is the nearest earlier peak where rises had stopped before; the price moved past it. Such moves often reverse. Informational only.",
  SUPPORT_BROKEN: "Support is the nearest earlier low where falls had stopped before; that level didn't hold. A price signal, not your thesis.",
  BREAKOUT: "A close above the previous 20 sessions' high on heavy volume: strong buying, a momentum signal, not a forecast. Informational only.",
  BREAKDOWN: "A close below the previous 20 sessions' low on heavy volume: strong selling, a momentum signal, not a forecast. For a long-term holding, price alone never leads to a sale.",
  LONG_TERM_TREND_BROKEN: "The price fell below its 200-day average. Now a default warning you can switch off per holding.",
  CONCENTRATION: "One position is above your portfolio weight limit; the app won't suggest adding to it.",
  SECTOR_CONCENTRATION: "One sector is above your sector limit; the app won't suggest adding to it.",
};

const TYPE_LABELS: Record<string, string> = {
  THESIS_CONDITION: "Thesis condition met",
  BUSINESS_CONDITION: "Business condition met",
  TECHNICAL_WARNING: "Technical warning",
  NEWS_MATERIAL: "Material company disclosure",
  LONG_TERM_TREND_BROKEN: "Long-term trend broken (before D7)",
  SUPPORT_BROKEN: "Support broken",
  BREAKDOWN: "Breakdown",
  RESISTANCE_CROSSED: "Above resistance",
  BREAKOUT: "Breakout",
  CONCENTRATION: "Position concentration",
  SECTOR_CONCENTRATION: "Sector concentration",
  EVENT_WINDOW: "Event approaching",
  SIGNAL_CHANGED: "Signal changed",
};

export default function Alerts({ onChange }: { onChange: () => void }) {
  const [showAll, setShowAll] = useState(false);
  const [severity, setSeverity] = useState("");
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const [ackErr, setAckErr] = useState<string>();
  const list = useApi<Alert[]>(`/api/alerts?open_only=${!showAll}`);
  const alerts = list.data, load = list.reload;
  const err = ackErr ?? (list.error && `Couldn't load alerts: ${list.error}`);

  const ack = async (id?: number) => {
    setAckErr(undefined);
    try {
      await api(id ? `/api/alerts/${id}/ack` : "/api/alerts/ack-all", { method: "POST" });
    } catch (e) {
      setAckErr(`Couldn't acknowledge: ${(e as Error).message}`);
      return;
    }
    await load();
    onChange();
  };

  const visible = useMemo(() => (alerts ?? []).filter((a) =>
    (!severity || a.severity === severity) && (!type || a.type === type) &&
    (!q || a.symbol.includes(q.toUpperCase()) || a.message.toLowerCase().includes(q.toLowerCase()))), [alerts, severity, type, q]);

  // An error after the first load is shown above the list, which stays usable (Retry by any action).
  if (!alerts) return err ? <p className="error">{err} <button className="secondary" onClick={load}>Retry</button></p>
    : <p>Loading…</p>;
  const openCount = alerts.filter((a) => !a.acknowledged_at).length;

  return (
    <>
    <section className="card">
      <div className="card-head">
        <h2>Alerts</h2>
        {openCount > 0 && <button onClick={() => ack()}>Acknowledge all ({openCount})</button>}
      </div>
      {err && <p className="error">{err}</p>}
      <p className="muted small">
        Raised when a condition <b>starts</b> between two snapshots, so each fires once. During market hours the backend
        refreshes every 15 minutes while logged in to Kite. Alerts ask for your attention; the app never trades.
      </p>
      <div className="filters">
        <input placeholder="Search symbol or message…" aria-label="Search alerts" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={severity} aria-label="Severity" onChange={(e) => setSeverity(e.target.value)}>
          <option value="">All severities</option>
          <option value="high">High</option><option value="medium">Medium</option><option value="info">Info</option>
        </select>
        <select value={type} aria-label="Alert type" onChange={(e) => setType(e.target.value)}>
          <option value="">All types</option>
          {Object.entries(TYPE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <label className="inline"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> Include acknowledged</label>
        <NotificationToggle />
      </div>
      {visible.length === 0 && <p className="muted">{showAll ? "No alerts yet." : "No open alerts."}</p>}
      {visible.length > 0 && (
        <p className="muted small">
          <b>Price signal</b> alerts come from the chart, not from your thesis; for long-term holdings they never
          require action. A <b>draft</b> tag means the app set that holding's horizon; confirm it on the stock page.
        </p>
      )}
      {(() => {
        const attention = visible.filter((a) => a.severity !== "info");
        const info = visible.filter((a) => a.severity === "info");
        return (
          <>
            {attention.length > 0 && <h3>Needs your attention ({attention.length})</h3>}
            <ul className="alerts">{attention.map((a) => <AlertRow key={a.id} a={a} onAck={ack} />)}</ul>
            {info.length > 0 && (
              <details open={attention.length === 0}>
                <summary><h3 style={{ display: "inline" }}>For information ({info.length})</h3></summary>
                <ul className="alerts">{info.map((a) => <AlertRow key={a.id} a={a} onAck={ack} />)}</ul>
              </details>
            )}
          </>
        );
      })()}
    </section>
    <TelegramCard />
    <section className="card">
      <h2>Default technical warnings <span className="muted small">portfolio-wide</span></h2>
      <WarningEditor />
      <p className="muted small">Switch one off or change its threshold for a single holding on that holding's page.</p>
    </section>
    </>
  );
}

/** One alert: headline and key numbers first, one line of what to do, the explanation on demand. */
function AlertRow({ a, onAck }: { a: Alert; onAck: (id: number) => void }) {
  const e = a.details?.explain;
  const d = a.details;
  return (
    <li className={`alert sev-${a.severity} ${a.acknowledged_at ? "acked" : ""}`}>
      <div className="alert-body">
        <div className="alert-head">
          <b>{a.symbol === "*" ? "Portfolio" : <a href={`#/stock/${a.symbol}`}>{a.symbol}</a>}</b>
          <span className="alert-title">{e?.headline ?? TYPE_LABELS[a.type] ?? a.type}</span>
          {d?.category && <span className="tag">{CATEGORY_CHIP[d.category] ?? d.category}</span>}
          {d?.horizon && <span className="chip-sm">{HORIZON_TAG[d.horizon] ?? d.horizon}{d.horizon_draft ? " · draft" : ""}</span>}
          <span className="muted small alert-time">{new Date(a.created_at).toLocaleString()}</span>
        </div>
        {e?.facts?.length ? (
          <div className="facts">
            {e.facts.map(([k, v]) => <span key={k} className="fact"><span className="muted">{k}</span> {v}</span>)}
          </div>
        ) : <div>{e?.what ?? a.message}</div>}
        {e ? (
          <>
            <div className="small"><span className="muted">What to do:</span> {e.action}
              {safeUrl(d?.url) && <> <a href={safeUrl(d?.url)} target="_blank" rel="noreferrer noopener">Open the disclosure</a></>}</div>
            <details className="small"><summary>Why?</summary>{e.meaning}{e.facts?.length ? <div className="muted">{e.what}</div> : null}</details>
          </>
        ) : TYPE_HELP[a.type] && <div className="small muted">{TYPE_HELP[a.type]}</div>}
      </div>
      {!a.acknowledged_at && <button className="secondary" onClick={() => onAck(a.id)}>Acknowledge</button>}
    </li>
  );
}

const NOTIFY_KEY = "alerts-notify";

export function notificationsEnabled(): boolean {
  try {
    return localStorage.getItem(NOTIFY_KEY) === "1" && "Notification" in window && Notification.permission === "granted";
  } catch {
    return false;
  }
}

function NotificationToggle() {
  const [on, setOn] = useState(notificationsEnabled());
  if (!("Notification" in window)) return null;
  const toggle = async () => {
    const next = !on;
    if (next && Notification.permission !== "granted" && (await Notification.requestPermission()) !== "granted") return;
    try { localStorage.setItem(NOTIFY_KEY, next ? "1" : "0"); } catch { /* storage unavailable */ }
    setOn(next);
  };
  return (
    <label className="inline" title="Shows a desktop notification for new high/medium alerts while this tab is open">
      <input type="checkbox" checked={on} onChange={toggle} /> Desktop notifications
    </label>
  );
}

type NotifyStatus = { token_set: boolean; linked: boolean; enabled: boolean; quiet_hours_ist: string; min_severity: string;
  summary_ist: string; last_error: string | null; chat_name: string | null; chat_type: string };

/** Telegram notifications: status and setup (bot token in backend/.env, then Link). */
export function TelegramCard() {
  const { data: s, error, reload: load } = useApi<NotifyStatus>("/api/notify/status");
  const [msg, setMsg] = useState<string>();
  const call = async (path: string, ok: string) => {
    setMsg(undefined);
    try { await api(path, { method: "POST" }); setMsg(ok); load(); } catch (e) { setMsg((e as Error).message); }
  };
  if (!s) return error ? (
    <section className="card"><h2>Telegram notifications</h2><p className="error">Couldn't load the status: {error}</p></section>
  ) : null;
  return (
    <section className="card">
      <h2>Telegram notifications <span className="muted small">{s.enabled ? "on" : "off"}</span></h2>
      {!s.token_set ? (
        <ol className="small">
          <li>In Telegram, open <b>@BotFather</b>, send <code>/newbot</code> and follow the steps. It gives you a token.</li>
          <li>Add <code>PI_TELEGRAM_BOT_TOKEN=&lt;token&gt;</code> to <code>backend/.env</code> and restart the backend.</li>
          <li>Open your new bot in Telegram and press <b>Start</b>, then come back here and press <b>Link Telegram</b>.</li>
        </ol>
      ) : !s.linked ? (
        <p className="small">Bot token found. Open your bot in Telegram, press <b>Start</b>, then:{" "}
          <button onClick={() => call("/api/notify/link", "Linked. A confirmation was sent to your Telegram.")}>Link Telegram</button></p>
      ) : (
        <>
          <p className="small">
            Sending <b>{s.min_severity === "medium" ? "high and medium" : "high"}</b> alerts to{" "}
            <b>{s.chat_type === "group" ? `the group “${s.chat_name}”` : `your chat${s.chat_name ? ` (${s.chat_name})` : ""}`}</b>;
            none between {s.quiet_hours_ist} IST (held until morning); a daily summary at {s.summary_ist} IST on weekdays.{" "}
            <button className="secondary" onClick={() => call("/api/notify/test", "Test message sent.")}>Send test</button>
          </p>
          <details className="small">
            <summary>Send to a group instead (several people)</summary>
            <ol>
              <li>In Telegram, create a group and add the people who should get alerts.</li>
              <li>Add your bot to the group (group info → Add members → search your bot's username).</li>
              <li>In the group, send <code>/start</code>.</li>
              <li>Press <button onClick={() => call("/api/notify/link", "Linked. A confirmation was sent to the chat.")}>Link again</button>{" "}
                within 24 hours. A group is picked over your private chat.</li>
            </ol>
            <p className="muted">Everyone in the group will see your holdings' names, signals and key numbers.</p>
          </details>
        </>
      )}
      {s.last_error && <p className="error small">Last send failed: {s.last_error} (will retry)</p>}
      {msg && <p className="muted small">{msg}</p>}
    </section>
  );
}
