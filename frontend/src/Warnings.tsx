import { useEffect, useState } from "react";
import { api, fmt, useApi } from "./api";
import { ValueInput } from "./ValueInput";

/** One default technical warning as the backend resolves it (portfolio setting + holding override). */
export type WarningState = {
  id: string; metric: string; op: string; value: number | string; enabled: boolean; description: string;
  default_value: number | string; portfolio_value: number | string; portfolio_enabled: boolean; overridden: boolean;
};
type Result = { id: string; result: "MET" | "NOT_MET" | "CANNOT_CHECK"; actual: number | string | null; reason: string };

const BADGE = {
  MET: <span className="signal s-REVIEW">Met</span>,
  NOT_MET: <span className="signal s-HOLD">Not met</span>,
  CANNOT_CHECK: <span className="signal">Cannot check</span>,
};
const show = (v: number | string | null) => (v == null ? "—" : typeof v === "number" ? fmt.pct(v) : v);

/**
 * Default technical warnings: generic thresholds, never your thesis. They raise an alert only
 * (info for long-term holdings, medium otherwise) and never trigger a review or a sale.
 * Without `symbol` it edits the portfolio-wide defaults; with it, this holding's overrides.
 */
export function WarningEditor({ symbol, results, onSaved }: { symbol?: string; results?: Result[] | null; onSaved?: () => void }) {
  const warnings = useApi<WarningState[]>(symbol ? `/api/warnings?symbol=${symbol}` : "/api/warnings");
  const rows = warnings.data;
  const [edit, setEdit] = useState<Record<string, { enabled: boolean; value: number | string }>>({});
  const [msg, setMsg] = useState<string>();
  // The form starts from what the backend resolved, again after every load or save.
  useEffect(() => {
    if (rows) setEdit(Object.fromEntries(rows.map((w) => [w.id, { enabled: w.enabled, value: w.value }])));
  }, [rows]);
  const apply = (r: WarningState[]) => warnings.setData(() => r);
  if (!rows) return warnings.error ? <p className="error">{warnings.error}</p> : null;
  if (rows.some((w) => !edit[w.id])) return null; // the form is filled on the next render

  // Only differences from the inherited value are stored, so later portfolio changes still apply.
  const save = async (reset = false) => {
    const body = reset ? {} : Object.fromEntries(rows.map((w) => {
      const e = edit[w.id];
      const inheritedEnabled = symbol ? w.portfolio_enabled : true;
      const inheritedValue = symbol ? w.portfolio_value : w.default_value;
      return [w.id, { enabled: e.enabled === inheritedEnabled ? null : e.enabled, value: e.value === inheritedValue ? null : e.value }];
    }));
    try {
      const saved = await api<WarningState[]>(symbol ? `/api/warnings/overrides/${symbol}` : "/api/warnings/defaults",
        { method: "PUT", body: JSON.stringify(body) });
      apply(saved);
      setMsg(reset ? "Reset to the portfolio defaults." : "Saved. Changing a threshold doesn't raise alerts by itself.");
      onSaved?.();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const dirty = rows.some((w) => edit[w.id] && (edit[w.id].enabled !== w.enabled || edit[w.id].value !== w.value));

  return (
    <>
      <p className="muted small">
        Default warnings, not your thesis. They apply to every holding, raise an alert only (info for long-term holdings,
        medium otherwise) and never cause a review or a sale.{symbol ? " Changes here apply to this holding only." : ""}
      </p>
      <ul className="checks">
        {rows.map((w) => {
          const e = edit[w.id];
          const r = results?.find((x) => x.id === w.id);
          return (
            <li key={w.id} className="filters">
              <label className="inline">
                <input type="checkbox" checked={e.enabled} onChange={(ev) => setEdit({ ...edit, [w.id]: { ...e, enabled: ev.target.checked } })} /> On
              </label>
              {symbol && (w.enabled ? r ? BADGE[r.result] : <span className="signal">After refresh</span> : <span className="signal">Off</span>)}
              <span>{w.description}</span>
              {typeof w.default_value === "number" && (
                <ValueInput key={`${w.id}-${w.value}`} metric={{ kind: "fraction" }} value={e.value}
                  onChange={(value) => setEdit({ ...edit, [w.id]: { ...e, value } })} />
              )}
              <span className="muted small">
                {symbol && r && w.enabled && (r.result === "CANNOT_CHECK" ? r.reason : `now ${show(r.actual)}`)}
                {symbol && w.overridden && " · custom for this holding"}
                {!symbol && typeof w.default_value === "number" && w.value !== w.default_value && ` · standard ${show(w.default_value)}`}
              </span>
            </li>
          );
        })}
      </ul>
      <button disabled={!dirty} onClick={() => save()}>Save</button>{" "}
      {symbol && rows.some((w) => w.overridden) && <button className="secondary" onClick={() => save(true)}>Reset to portfolio defaults</button>}
      {msg && <span className="muted small"> {msg}</span>}
    </>
  );
}
