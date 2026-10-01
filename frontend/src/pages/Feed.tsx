import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";

type Stock = { symbol: string; name: string | null; held: boolean; view: "BUY" | "SELL" | null; from_image?: boolean };
type Post = {
  key: string; channel: string; channel_title: string | null; id: number; url: string; date: string | null;
  text: string; views: string | null; media: boolean; holdings: string[]; stocks: Stock[];
  image_text: string | null; image_status: "read" | "pending" | "failed" | "off" | null;
  same_story_as: string | null; // a newer post on the same story (backend telegram_feed.group_same_story)
};

const VIEW_WORDS = { BUY: "the post reads as buy / invest", SELL: "the post reads as sell / exit" };
type FeedResponse = {
  channels: string[]; titles: Record<string, string | null>; older: Record<string, number | null>;
  errors: Record<string, string>; posts: Post[];
};

const FOLD_LINES = 8; // press releases read from images run to 40+ lines: show the top, the rest on request

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

function ImageText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  const folded = !open && lines.length > FOLD_LINES + 2; // folding away one or two lines saves nothing
  return (
    <div className="feed-image-text">
      <span className="muted small">Text read from the image</span>
      <div className="feed-text">{folded ? lines.slice(0, FOLD_LINES).join("\n") : text}</div>
      {folded && <button className="link small" onClick={() => setOpen(true)}>Show all {lines.length} lines</button>}
    </div>
  );
}

function PostCard({ p, repeats = [] }: { p: Post; repeats?: Post[] }) {
  const [open, setOpen] = useState(false);
  const about = [...new Set(repeats.flatMap((r) => r.stocks.map((s) => s.symbol)))].join(", ");
  return (
    <div className={`feed-post${p.holdings.length ? " feed-held" : ""}`}>
      <div className="feed-meta">
        <b>{p.channel_title ?? p.channel}</b>
        <span className="muted small">{when(p.date)}{p.views ? ` · ${p.views} views` : ""}</span>
        {p.holdings.length > 0 && <span className="tag">names your holding</span>}
        <a className="small" href={p.url} target="_blank" rel="noopener noreferrer">Open in Telegram ↗</a>
      </div>
      {p.stocks.length > 0 && (
        <div className="feed-stocks">
          {p.stocks.map((s) => {
            const tip = `${s.name ?? s.symbol}${s.held ? " · you hold this" : ""}${s.view ? ` · ${VIEW_WORDS[s.view]}` : ""}`;
            return s.held
              ? <a key={s.symbol} href={`#/stock/${s.symbol}`} title={tip} className={`stock held stock-${s.view ?? "NONE"}`}>{s.symbol}</a>
              : <span key={s.symbol} title={tip} className={`stock stock-${s.view ?? "NONE"}`}>{s.symbol}</span>;
          })}
        </div>
      )}
      {p.text && <div className="feed-text">{p.text}</div>}
      {p.image_text && <ImageText text={p.image_text} />}
      {p.image_status === "pending" && <div className="muted small">Reading the image…</div>}
      {p.image_status === "read" && !p.image_text && !p.text && (
        <div className="muted small">The image has no readable English text: open it in Telegram.</div>
      )}
      {p.image_status === "failed" && <div className="muted small">Couldn't read the image; retried later. Open it in Telegram.</div>}
      {p.image_status === "off" && (
        <div className="muted small">Has a photo. Reading images is off (needs the backend's "ocr" extra): open it in Telegram.</div>
      )}
      {p.media && !p.image_status && !p.text && <div className="muted small">Has a video or file: open it in Telegram.</div>}
      {repeats.length > 0 && (
        <div className="feed-repeats">
          <button className="link small" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} {repeats.length} earlier post{repeats.length === 1 ? "" : "s"} on this story
            {about ? ` (${about})` : ""}
          </button>
          {open && repeats.map((r) => <PostCard key={r.key} p={r} />)}
        </div>
      )}
    </div>
  );
}

/** Public Telegram channels, read-only. Posts are shown as written: unverified, never used in signals. */
export default function Feed() {
  const [data, setData] = useState<FeedResponse>();
  const [older, setOlder] = useState<Post[]>([]);
  const [olderFrom, setOlderFrom] = useState<number | null>(null);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState("");
  const [channel, setChannel] = useState("");
  const [heldOnly, setHeldOnly] = useState(false);
  const [stocksOnly, setStocksOnly] = useState(false);
  const [q, setQ] = useState("");

  // Only the newest request may update the page: a slow reply (or a background refresh) for the
  // previous channel must not land after you picked another one.
  const latest = useRef(0);
  const [polls, setPolls] = useState(0);
  const load = async (ch = channel, quiet = false) => {
    const mine = ++latest.current;
    if (!quiet) setBusy(true);
    setErr(undefined);
    try {
      const d = await api<FeedResponse>(`/api/feed${ch ? `?channel=${encodeURIComponent(ch)}` : ""}`);
      if (mine !== latest.current) return;
      setData(d);
      if (!quiet) {
        setOlder([]);
        setOlderFrom(ch ? d.older[ch] ?? null : null);
      }
    } catch (e) {
      if (mine === latest.current) setErr(String(e));
    } finally {
      if (!quiet) setBusy(false);
      else setPolls((n) => n + 1); // a failed refresh schedules the next one too
    }
  };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Images are read in the background (a few seconds each): refresh until they are all done.
  const reading = (data?.posts ?? []).filter((p) => p.image_status === "pending").length;
  useEffect(() => {
    if (!reading) return;
    const t = setTimeout(() => load(channel, true), 8000);
    return () => clearTimeout(t);
  }, [data, channel, polls]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = async () => {
    if (!adding.trim()) return;
    setBusy(true);
    setErr(undefined);
    try {
      await api("/api/feed/channels", { method: "POST", body: JSON.stringify({ channel: adding }) });
      setAdding("");
      await load();
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  };

  const remove = async (name: string) => {
    await api(`/api/feed/channels/${encodeURIComponent(name)}`, { method: "DELETE" }).catch((e) => setErr(String(e)));
    if (channel === name) setChannel("");
    await load(channel === name ? "" : channel);
  };

  const loadOlder = async () => {
    if (!channel || olderFrom == null) return;
    setBusy(true);
    try {
      const d = await api<FeedResponse>(`/api/feed?channel=${encodeURIComponent(channel)}&before=${olderFrom}`);
      setOlder((o) => [...o, ...d.posts]);
      setOlderFrom(d.older[channel] ?? null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const posts = useMemo(() => {
    const all = [...(data?.posts ?? []), ...older];
    const needle = q.trim().toLowerCase();
    return all.filter((p) => (!heldOnly || p.holdings.length > 0) && (!stocksOnly || p.stocks.length > 0)
      && (!needle || `${p.text}\n${p.image_text ?? ""}`.toLowerCase().includes(needle)));
  }, [data, older, heldOnly, stocksOnly, q]);
  // Repeats fold under the newest post of their story, when that post is shown too (a filter can hide it).
  const stories = useMemo(() => {
    const shown = new Set(posts.map((p) => p.key));
    const repeats = new Map<string, Post[]>();
    for (const p of posts)
      if (p.same_story_as && shown.has(p.same_story_as))
        repeats.set(p.same_story_as, [...(repeats.get(p.same_story_as) ?? []), p]);
    return posts.filter((p) => !(p.same_story_as && shown.has(p.same_story_as)))
      .map((head) => ({ head, repeats: repeats.get(head.key) ?? [] }));
  }, [posts]);
  const loaded = [...(data?.posts ?? []), ...older];
  const mentioning = loaded.filter((p) => p.holdings.length).length;
  const withStocks = loaded.filter((p) => p.stocks.length).length;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <h2>Telegram feed</h2>
          <span className="chip-sm">Unverified · not used in signals or alerts</span>
        </div>
        <p className="muted small">
          Latest posts from public Telegram channels, read without logging in. Holdings named in a post (by NSE symbol,
          e.g. IRCTC or #IRCTC) are highlighted. Posts are opinions, often from people not registered with SEBI: check a
          stock's page before acting on anything here.
        </p>
        <div className="filters">
          <input placeholder="Add a public channel: name, @name or t.me link" aria-label="Channel to add" value={adding}
                 onChange={(e) => setAdding(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
          <button disabled={busy || !adding.trim()} onClick={add}>Add channel</button>
        </div>
        {data && data.channels.length > 0 && (
          <div className="chips">
            {data.channels.map((c) => (
              <span key={c} className="chip-sm">
                {data.titles[c] ?? c} <span className="muted">@{c}</span>{" "}
                <button className="link" onClick={() => remove(c)} title={`Remove ${c}`} aria-label={`Remove ${c}`}>✕</button>
              </span>
            ))}
          </div>
        )}
        {data && Object.entries(data.errors).map(([c, m]) => <div key={c} className="error small">{c}: {m}</div>)}
        {err && <div className="error">{err}</div>}
      </div>

      {data && data.channels.length === 0 && (
        <div className="card muted">No channels yet. Add a public channel above to see its latest posts here.</div>
      )}

      {data && data.channels.length > 0 && (
        <div className="card">
          <div className="filters">
            <select value={channel} onChange={(e) => { setChannel(e.target.value); load(e.target.value); }}>
              <option value="">All channels</option>
              {data.channels.map((c) => <option key={c} value={c}>{data.titles[c] ?? c}</option>)}
            </select>
            <input placeholder="Search posts" aria-label="Search posts" value={q} onChange={(e) => setQ(e.target.value)} />
            <label className="inline">
              <input type="checkbox" checked={heldOnly} onChange={(e) => setHeldOnly(e.target.checked)} /> Only my
              holdings ({mentioning})
            </label>
            <label className="inline">
              <input type="checkbox" checked={stocksOnly} onChange={(e) => setStocksOnly(e.target.checked)} /> Only posts
              naming a stock ({withStocks})
            </label>
            <button className="secondary" disabled={busy} onClick={() => load()}>{busy ? "Loading…" : "Refresh"}</button>
          </div>
          <p className="small">
            Stocks named in a post: <span className="stock stock-BUY">green</span> the post reads as buy or invest,{" "}
            <span className="stock stock-SELL">red</span> sell or exit, <span className="stock">grey</span> news or
            unclear. <b>Bold</b> = you hold it. Read from the post's words, not checked by the app.
          </p>
          <p className="muted small">
            {posts.length} post{posts.length === 1 ? "" : "s"} shown
            {stories.length < posts.length ? ` as ${stories.length} stories` : ""}
            {channel ? "" : " (the latest page from each channel; pick one channel to go further back)"}
            {reading ? `. Reading ${reading} image${reading === 1 ? "" : "s"} (offline, a few seconds each)…` : ""}. Fetched at most
            every 5 minutes.
          </p>

          {stories.map(({ head, repeats }) => <PostCard key={head.key} p={head} repeats={repeats} />)}

          {channel && olderFrom != null && (
            <button className="secondary" disabled={busy} onClick={loadOlder}>{busy ? "Loading…" : "Older posts"}</button>
          )}
        </div>
      )}
    </>
  );
}
