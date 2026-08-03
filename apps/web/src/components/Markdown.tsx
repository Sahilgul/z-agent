import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

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
