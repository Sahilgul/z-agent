import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** GFM markdown in Patch Bay ink — plans, notebooks, Lead messages.
 *  Typography rules live in the `.md` component layer of theme/index.css. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
