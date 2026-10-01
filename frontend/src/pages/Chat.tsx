import { useState } from "react";
import { api } from "../api";
import { Markdown } from "../Markdown";

type Msg = { role: "user" | "assistant"; text: string; meta?: string };
const SUGGESTIONS = ["How is my portfolio doing?", "What changed this week?", "Which holdings need attention?"];

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  // One question at a time: answers would otherwise land in arrival order, under the wrong question.
  const send = async (text: string) => {
    if (!text.trim() || busy) return;
    setMsgs((m) => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const r = await api<{ text: string; model: string; grounded: boolean; ungrounded_numbers: string[] }>(
        "/api/chat", { method: "POST", body: JSON.stringify({ message: text }) });
      setMsgs((m) => [...m, { role: "assistant", text: r.text,
        meta: `${r.model} · ${r.grounded ? "grounded ✓" : "UNGROUNDED: " + r.ungrounded_numbers.join(", ")}` }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", text: String(e) }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card chat">
      <h2>Ask about your portfolio</h2>
      <div className="suggestions">
        {SUGGESTIONS.map((s) => <button key={s} disabled={busy} onClick={() => send(s)}>{s}</button>)}
      </div>
      {msgs.map((m, i) => (
        <div key={i} className={`msg ${m.role}`}>
          {m.role === "assistant" ? <Markdown text={m.text} /> : <div>{m.text}</div>}
          {m.meta && <div className="muted small">{m.meta}</div>}
        </div>
      ))}
      <form onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask a question…" aria-label="Question"
               disabled={busy} />
        <button disabled={busy}>{busy ? "…" : "Send"}</button>
      </form>
    </section>
  );
}
