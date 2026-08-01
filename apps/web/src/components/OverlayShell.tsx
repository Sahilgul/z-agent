import type { ReactNode } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useUi } from "../stores/ui";

/** 80% overlay shell on the Dialog primitive (focus trap + ESC built in):
 *  the monitor keeps streaming underneath; close returns to a live screen,
 *  never a stale one. Entrance is motion moment #1. */
export function OverlayShell({ title, children }: { title: string; children: ReactNode }) {
  const popOverlay = useUi((s) => s.popOverlay);
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && popOverlay()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-overlay bg-jack/55 backdrop-blur-[3px]" />
        <DialogPrimitive.Popup className="animate-enter fixed left-1/2 top-1/2 z-overlay flex h-[82%] w-[80%] -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-hairline bg-bg-panel shadow-overlay outline-none max-md:h-[calc(100%-2rem)] max-md:w-[calc(100%-2rem)]">
          <div className="flex flex-none items-center justify-between border-b border-hairline px-s4 py-s3">
            <DialogPrimitive.Title className="font-mono text-[12.5px] font-semibold tracking-[0.03em] text-ink-secondary">
              {title}
            </DialogPrimitive.Title>
            <DialogPrimitive.Close
              render={<Button variant="ghost" size="sm" title="close overlay" aria-label="close overlay" className="font-mono" />}
            >
              <XIcon aria-hidden="true" />
              close
            </DialogPrimitive.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-s4">{children}</div>
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
