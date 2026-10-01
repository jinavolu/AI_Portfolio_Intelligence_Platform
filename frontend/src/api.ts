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
