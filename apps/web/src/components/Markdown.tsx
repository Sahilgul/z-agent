import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** GFM markdown in Patch Bay ink — plans, notebooks, Lead messages. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
      <style>{`
        .md { font-size: 14px; line-height: 1.65; color: var(--ink-primary); }
        .md h1, .md h2, .md h3 { font-family: var(--font-display); font-weight: 500; margin: 18px 0 8px; }
        .md h1 { font-size: 22px; } .md h2 { font-size: 18px; } .md h3 { font-size: 15px; }
        .md p { margin: 8px 0; }
        .md code { font-family: var(--font-mono); font-size: 12.5px; background: var(--jack); padding: 1px 5px; border-radius: 4px; color: var(--blue-bright); }
        .md pre { background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius); padding: 12px 14px; overflow-x: auto; }
        .md pre code { background: none; padding: 0; color: var(--ink-primary); }
        .md ul, .md ol { padding-left: 22px; margin: 8px 0; }
        .md li { margin: 4px 0; }
        .md table { border-collapse: collapse; width: 100%; margin: 12px 0; }
        .md th, .md td { border: 1px solid var(--hairline); padding: 6px 10px; font-size: 13px; text-align: left; }
        .md th { font-family: var(--font-mono); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-secondary); background: var(--bg-module); }
        .md a { color: var(--blue-bright); }
        .md blockquote { border-left: 2px solid var(--green); margin: 10px 0; padding: 2px 14px; color: var(--ink-secondary); }
      `}</style>
    </div>
  );
}
