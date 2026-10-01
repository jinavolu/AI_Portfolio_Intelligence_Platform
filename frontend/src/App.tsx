import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import BrokerBanner from "./BrokerBanner";
import Alerts, { Alert, notificationsEnabled } from "./pages/Alerts";
import Backtest from "./pages/Backtest";
import Chat from "./pages/Chat";
import Dashboard from "./pages/Dashboard";
import Feed from "./pages/Feed";
import Scanner from "./pages/Scanner";
import Holdings from "./pages/Holdings";
import Stock from "./pages/Stock";

type Page = "dashboard" | "holdings" | "scanner" | "alerts" | "feed" | "backtest" | "chat";
type Route = { page: Page } | { page: "stock"; symbol: string };
type Counts = { total: number; high?: number; medium?: number; info?: number };

function parse(hash: string): Route {
  const [page, symbol] = hash.replace(/^#\/?/, "").split("/");
  if (page === "stock" && symbol) return { page: "stock", symbol };
  if (page === "holdings" || page === "scanner" || page === "chat" || page === "alerts" || page === "feed"
    || page === "backtest") return { page };
  return { page: "dashboard" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(parse(location.hash));
  const [counts, setCounts] = useState<Counts>({ total: 0 });
  const [account, setAccount] = useState<{ id: string; connected: boolean }>();
  const lastSeenId = useRef<number | null>(null);

  // Whose data is on screen: theses, snapshots and alerts are kept per Kite user.
  useEffect(() => {
    api<{ account?: string; connected: boolean }>("/api/broker/status")
      .then((s) => s.account && s.account !== "default" && setAccount({ id: s.account, connected: s.connected }))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);

  // Poll the open-alert count; notify on alerts newer than the last one seen in this tab.
  const refreshAlerts = useCallback(async () => {
    try {
      setCounts(await api<Counts>("/api/alerts/count"));
      const open = await api<Alert[]>("/api/alerts?open_only=true&limit=50");
      const maxId = open.reduce((m, a) => Math.max(m, a.id), 0);
      if (lastSeenId.current !== null && notificationsEnabled()) {
        open.filter((a) => a.id > lastSeenId.current! && a.severity !== "info").slice(0, 5).forEach((a) =>
          new Notification(`${a.symbol === "*" ? "Portfolio" : a.symbol}: ${a.severity} alert`, { body: a.message, tag: `alert-${a.id}` }));
      }
      lastSeenId.current = Math.max(lastSeenId.current ?? 0, maxId);
    } catch {
      /* backend unavailable or broker logged out; try again next tick */
    }
  }, []);
  useEffect(() => {
    refreshAlerts();
    const t = setInterval(refreshAlerts, 60_000);
    return () => clearInterval(t);
  }, [refreshAlerts]);

  const link = (page: Page, label: React.ReactNode) => (
    <a href={`#/${page}`} className={route.page === page ? "active" : ""}>
      {label}
    </a>
  );

  return (
    <>
      <header>
        <strong>Portfolio Intelligence</strong>
        <nav>
          {link("dashboard", "Dashboard")}
          {link("holdings", "Holdings")}
          {link("scanner", "Scanner")}
          {link("alerts", <>Alerts{counts.total > 0 && <span className={`count ${counts.high ? "high" : ""}`}>{counts.total}</span>}</>)}
          {link("feed", "Feed")}
          {link("backtest", "Backtest")}
          {link("chat", "Chat")}
        </nav>
        {account && (
          <span className="badge" title={account.connected ? "Logged in to Kite as this user"
            : "Not logged in; showing this user's last data"}>
            Kite: {account.id}{account.connected ? "" : " (logged out)"}
          </span>
        )}
        <span className="badge">read-only · signals only</span>
      </header>
      <main>
        <BrokerBanner />
        {route.page === "dashboard" && <Dashboard />}
        {route.page === "holdings" && <Holdings />}
        {route.page === "alerts" && <Alerts onChange={refreshAlerts} />}
        {route.page === "scanner" && <Scanner />}
        {route.page === "feed" && <Feed />}
        {route.page === "backtest" && <Backtest />}
        {route.page === "chat" && <Chat />}
        {/* key: a fresh page per stock, so one stock's error, data or explanation never shows under another */}
        {route.page === "stock" && <Stock key={route.symbol} symbol={route.symbol} />}
      </main>
    </>
  );
}
