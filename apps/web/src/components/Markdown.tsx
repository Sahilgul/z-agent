import { isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeView } from "./CodeView";
import { Mermaid } from "./Mermaid";

/** GFM markdown in Patch Bay ink — plans, notebooks, Lead messages.
 *  Typography rules live in the `.md` component layer of theme/index.css. */
const COMPONENTS: Components = {
  // A many-column table is the one payload that legitimately exceeds the
  // bubble. The stream pane clips its horizontal axis (so a wide payload can
  // never drag the app sideways), which means an unwrapped table would be cut
  // off with no way to reach the rest — it has to own a scroller.
  table: ({ children, ...props }) => (
    <div className="md-scroll">
      <table {...props}>{children}</table>
    </div>
  ),
  // Fenced blocks render as VS Code-themed code (CodeView); the fence info
  // string ("```ts") arrives as the code child's language-ts className.
  // Mermaid fences branch to the lazy Mermaid renderer instead. Inline `code`
  // never passes through pre, so it keeps its `.md` styling.
  pre: ({ children, ...props }) => {
    if (!isValidElement<{ className?: string; children?: ReactNode }>(children)) {
      return <pre {...props}>{children}</pre>;
    }
    const lang = /language-(\w+)/.exec(children.props.className ?? "")?.[1];
    const code = typeof children.props.children === "string" ? children.props.children : "";
    if (lang === "mermaid") {
      return <Mermaid code={code} />;
    }
    return <CodeView code={code} lang={lang} />;
  },
};

export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
