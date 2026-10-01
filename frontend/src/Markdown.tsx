import { Fragment, type ReactNode } from "react";

/** **bold** inside a line; everything else stays text (React escapes it). */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : <Fragment key={i}>{part}</Fragment>);
}

/**
 * The small Markdown subset the AI answers use: paragraphs, "- " / "1. " lists, "#" headings (shown as
 * bold lines) and **bold**. Builds React elements, never HTML, so model output can't inject markup.
 */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (para.length) blocks.push(<p key={blocks.length}>{inline(para.join(" "))}</p>);
    if (list) {
      const items = list.items.map((it, i) => <li key={i}>{inline(it)}</li>);
      blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>);
    }
    para = [];
    list = null;
  };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    const numbered = line.match(/^\d+[.)]\s+(.*)$/);
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (!line) {
      flush();
    } else if (bullet || numbered) {
      const ordered = !bullet;
      if (para.length || (list && list.ordered !== ordered)) flush();
      list = list ?? { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]);
    } else if (heading) {
      flush();
      blocks.push(<p key={blocks.length}><strong>{heading[1].replace(/\*\*/g, "")}</strong></p>);
    } else {
      if (list) flush();
      para.push(line);
    }
  }
  flush();
  return <div className="md">{blocks}</div>;
}
