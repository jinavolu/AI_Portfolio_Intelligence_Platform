import { useEffect, useRef, useState } from "react";
import { api } from "./api";

type Status = { broker: string; connected: boolean; awaiting_login?: boolean; detail?: string; login_supported?: boolean };

/** Kite login prompt (tokens expire daily). Follows the flow verified in Phase 0: fresh session → login link →
 *  NO broker calls while the user logs in → user confirms → data calls. */
export default function BrokerBanner() {
  const [status, setStatus] = useState<Status>();
  const [err, setErr] = useState<string>();
  const busy = useRef(false);

  useEffect(() => {
    api<Status>("/api/broker/status").then(setStatus).catch((e) => setErr(String(e)));
  }, []);

  // No status at all: the backend itself isn't answering (not started, or crashed). Say so.
  if (!status) return err ? (
    <div className="banner">
      <strong>Can't reach the backend.</strong>
      <span className="muted">Start it (README: <code>uv run uvicorn app.main:create_app --factory --reload</code> in
        backend/), then reload this page. ({err})</span>
    </div>
  ) : null;
  if (status.connected) return null;

  const run = async (fn: () => Promise<void>) => {
    if (busy.current) return; // a second login request would invalidate the first link
    busy.current = true;
    setErr(undefined);
    try {
      await fn();
    } catch (e) {
      setErr(String(e));
    } finally {
      busy.current = false;
    }
  };

  const login = () => run(async () => {
    // Open the tab now, while the click still counts as the user's: a window opened after an await is
    // often caught by popup blockers. It is pointed at the login link once the backend returns it.
    const tab = window.open("", "_blank");
    try {
      const { login_url } = await api<{ login_url: string }>("/api/broker/login", { method: "POST" });
      if (tab) {
        tab.opener = null; // the Zerodha page gets no handle on this app
        tab.location.href = login_url;
      } else {
        window.open(login_url, "_blank", "noopener");
      }
      setStatus({ ...status, awaiting_login: true });
    } catch (e) {
      tab?.close();
      throw e;
    }
  });

  const confirm = () => run(async () => {
    const s = await api<Status>("/api/broker/confirm-login", { method: "POST" });
    if (s.connected) location.reload();
    else {
      setStatus(s);
      setErr(`Still not connected: ${s.detail}. Click "Log in to Kite" to get a fresh link.`);
    }
  });

  return (
    <div className="banner">
      <strong>Not connected to {status.broker === "kite_mcp" ? "Kite" : status.broker}.</strong>
      {!status.login_supported && <span className="muted">{status.detail}</span>}
      {status.login_supported && !status.awaiting_login && <button onClick={login}>Log in to Kite</button>}
      {status.login_supported && status.awaiting_login && (
        <>
          <span>Finish the Zerodha login in the new tab until it says "Your login was successful", then</span>
          <button onClick={confirm}>I've finished logging in</button>
          <button className="secondary" onClick={login}>Get a new link</button>
        </>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  );
}
