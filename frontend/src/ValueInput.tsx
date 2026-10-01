import { useState } from "react";

export type ValueKind = { kind: "enum" | "fraction" | "number"; values?: string[] | null };

/** A condition threshold. Fractions are typed as percentages: -25 means -25% (stored as -0.25). */
export function ValueInput({ metric, value, onChange }: { metric: ValueKind; value: number | string; onChange: (v: number | string) => void }) {
  const toText = (v: number | string) => (typeof v === "number" && metric.kind === "fraction" ? String(+(v * 100).toFixed(4)) : String(v));
  const [text, setText] = useState(toText(value));
  if (metric.kind === "enum") {
    return (
      <select value={String(value)} aria-label="Threshold" onChange={(e) => onChange(e.target.value)}>
        {metric.values!.map((v) => <option key={v}>{v}</option>)}
      </select>
    );
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
      <input className="num" style={{ width: 90, margin: 0 }} value={text}
             aria-label={metric.kind === "fraction" ? "Threshold (%)" : "Threshold"} onChange={(e) => {
        setText(e.target.value);
        const n = Number(e.target.value);
        if (e.target.value.trim() !== "" && !Number.isNaN(n)) onChange(metric.kind === "fraction" ? n / 100 : n);
      }} />
      {metric.kind === "fraction" && " %"}
    </span>
  );
}
