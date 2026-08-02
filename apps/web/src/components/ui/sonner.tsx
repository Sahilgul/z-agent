import { Toaster as SonnerToaster, toast } from "sonner";

/** Sonner toaster themed to the locked tokens. Mounted once at the app
 *  root (z-toast). Success → green-bright, error → danger-bright, default
 *  → panel/hairline. This delivers the optimistic-mutation feedback
 *  channel the design doc claims but lacked. */
export function Toaster() {
  return (
    <SonnerToaster
      position="bottom-right"
      toastOptions={{
        classNames: {
          toast:
            "group toast font-mono text-[12.5px] rounded-md border border-hairline bg-bg-panel text-ink-primary shadow-pop",
          title: "text-ink-primary font-semibold",
          description: "text-ink-secondary",
          actionButton: "bg-green text-ink-on-green",
          cancelButton: "bg-bg-module text-ink-secondary",
          success: "border-green/40 text-ok-bright",
          error: "border-danger/40 text-danger-bright",
          warning: "border-warn/40 text-warn-bright",
        },
      }}
      closeButton
      richColors={false}
    />
  );
}

export { toast };
