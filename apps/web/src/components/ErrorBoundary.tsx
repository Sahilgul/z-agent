import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { useAppTranslation } from "@/i18n";

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
  message: string;
}

/** Top-level error boundary — prevents a single screen crash from
 *  whitescreening the whole app. Renders a token-styled fallback with a
 *  reload action; runs are safe server-side. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: "" };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return <ErrorFallback message={this.state.message} />;
    }
    return this.props.children;
  }
}

function ErrorFallback({ message }: { message: string }) {
  const { t } = useAppTranslation();
  return (
    <div className="flex h-full items-center justify-center p-s8">
      <div className="w-full max-w-[440px] rounded-lg border border-hairline bg-bg-panel p-s8 shadow-overlay">
        <div className="mb-s3 flex items-center gap-s2">
          <span className="led led--red" aria-hidden="true" />
          <span className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-danger-bright">
            {t("errBoundary.title")}
          </span>
        </div>
        <p className="mb-s2 text-[14px] leading-[1.55] text-ink-primary">{t("errBoundary.body")}</p>
        {message && (
          <pre className="mb-s4 max-h-[120px] overflow-auto rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] text-ink-secondary">
            {message}
          </pre>
        )}
        <Button className="font-mono" onClick={() => window.location.reload()}>
          {t("errBoundary.reload")}
        </Button>
      </div>
    </div>
  );
}
