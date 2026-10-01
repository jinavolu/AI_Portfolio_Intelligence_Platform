import { useCallback, useEffect, useRef, useState } from "react";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(errorText(body.detail) ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

/** FastAPI's `detail`: a plain message, or for a 422 a list of {loc, msg}. Shown without JSON quotes. */
export function errorText(detail: unknown): string | null {
  if (detail == null || detail === "") return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : JSON.stringify(d))).join("; ");
  return JSON.stringify(detail);
}

export type ApiState<T> = {
  data: T | undefined;
  /** The last request's error. `data` keeps the last good reply, so a page can show both. */
  error: string | undefined;
  /** True until the first reply for the current url (success or not); a reload or poll keeps it false. */
  loading: boolean;
  reload: () => Promise<void>;
  setData: (update: (d: T | undefined) => T | undefined) => void;
};

/** GET `url` on mount and whenever it changes (null: don't fetch). Every page loads through this, so all of
 *  them get the same behaviour:
 *  - only the newest request may update the page: a slow reply for the previous stock, channel or range is
 *    dropped instead of overwriting the current one;
 *  - an error keeps the last good data and is shown beside it;
 *  - `poll(data)` returns milliseconds until the next refresh, or null to stop. It is asked again after every
 *    reply, failed ones included, so one network error doesn't end the polling;
 *  - nothing updates after the page is closed. */
export function useApi<T>(url: string | null, opts: { poll?: (data: T | undefined) => number | null } = {}): ApiState<T> {
  const [data, setDataState] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(url != null);
  const [replies, setReplies] = useState(0);
  const latest = useRef(0);
  const alive = useRef(true);
  const poll = useRef(opts.poll);
  poll.current = opts.poll;

  const reload = useCallback(async () => {
    if (url == null) return;
    const mine = ++latest.current;
    try {
      const d = await api<T>(url);
      if (alive.current && mine === latest.current) {
        setDataState(d);
        setError(undefined);
      }
    } catch (e) {
      if (alive.current && mine === latest.current) setError((e as Error).message);
    } finally {
      if (alive.current && mine === latest.current) {
        setLoading(false);
        setReplies((n) => n + 1);
      }
    }
  }, [url]);

  useEffect(() => {
    alive.current = true;
    // Another url is another thing: don't show the previous one's data or error under the new label.
    setDataState(undefined);
    setError(undefined);
    setLoading(url != null);
    reload();
    return () => { alive.current = false; latest.current++; };
  }, [reload, url]);

  useEffect(() => {
    const ms = poll.current?.(data);
    if (ms == null || url == null) return;
    const t = setTimeout(reload, ms);
    return () => clearTimeout(t);
  }, [replies, reload]); // eslint-disable-line react-hooks/exhaustive-deps -- re-armed per reply

  const setData = useCallback((update: (d: T | undefined) => T | undefined) => setDataState(update), []);
  return { data, error, loading, reload, setData };
}

/** Only web links from outside data (NSE filings, search results) become hrefs: never javascript: or data:. */
export const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : undefined);

const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

export const fmt = {
  inr: (v: number | null | undefined) => (v == null ? "—" : `₹${inr.format(v)}`),
  num: (v: number | null | undefined) => (v == null ? "—" : inr.format(v)),
  pct: (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(2)}%`),
  sign: (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "pos" : "neg"),
};

export type Holding = {
  symbol: string;
  sector: string;
  quantity: number;
  average_price: number;
  last_price: number;
  pnl: number;
  pnl_pct: number | null;
  day_change_pct: number | null;
  weight: number;
  trend: string | null;
  signal: string;
  dont_add: boolean;
  reason: string;
  summary: string;
  as_of: Record<string, string | null>;
  thesis: { version: number; horizon: string | null; draft: boolean } | null;
};
