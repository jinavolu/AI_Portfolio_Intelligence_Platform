import { useState } from "react";
import { api } from "./api";

/** Apply one draft thesis to many holdings. Drafts satisfy the thesis gate so signals appear,
 *  but stay flagged until each is edited on its Stock page. */
export default function BulkThesis({ symbols, onDone, onCancel }: {
  symbols: string[];
  onDone: (msg: string) => void;
  onCancel: () => void;
}) {
  const [horizon, setHorizon] = useState("LONG_TERM");
  const [why, setWhy] = useState("");
  const [assumptions, setAssumptions] = useState("");
  const [skipExisting, setSkipExisting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  const submit = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await api<{ saved: string[]; skipped: string[] }>("/api/theses/bulk", {
        method: "POST",
        body: JSON.stringify({
          symbols,
          skip_existing: skipExisting,
          thesis: {
            why_bought: why.trim(),
            horizon,
            assumptions: assumptions.split("\n").map((s) => s.trim()).filter(Boolean),
            metrics_to_monitor: [],
            invalidation_conditions: [],
          },
        }),
      });
      onDone(`Draft thesis saved for ${r.saved.length} holding(s)` +
        (r.skipped.length ? `; skipped ${r.skipped.length} that already had one (${r.skipped.join(", ")})` : ""));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bulk">
      <h3>Draft thesis for {symbols.length} selected holding{symbols.length === 1 ? "" : "s"}</h3>
      <div className="bulk-grid">
        <label>Horizon
          <select value={horizon} onChange={(e) => setHorizon(e.target.value)}>
            <option>SHORT_TERM</option><option>MEDIUM_TERM</option><option>LONG_TERM</option>
          </select>
        </label>
        <label>Why I hold these (shared)
          <input value={why} onChange={(e) => setWhy(e.target.value)} placeholder="e.g. Core long-term compounders" />
        </label>
      </div>
      <label>Key assumptions (optional, one per line)
        <textarea rows={2} value={assumptions} onChange={(e) => setAssumptions(e.target.value)} />
      </label>
      <label className="inline">
        <input type="checkbox" checked={skipExisting} onChange={(e) => setSkipExisting(e.target.checked)} />
        Skip holdings that already have a thesis (recommended)
      </label>
      <p className="muted small">
        Saved as <b>drafts</b>: they unlock signals now, but have no invalidation conditions, so gate 4 can't
        send them to REVIEW. Refine each one on its Stock page.
      </p>
      <button disabled={busy || !why.trim()} onClick={submit}>{busy ? "Saving…" : "Apply draft thesis"}</button>{" "}
      <button className="secondary" onClick={onCancel}>Cancel</button>
      {err && <p className="error">{err}</p>}
    </div>
  );
}
