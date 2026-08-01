import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Screen header — Fraunces title once per screen, mono-caps sub-label,
 *  actions slot on the right. */
export function PageHead({
  title,
  sub,
  actions,
  className,
}: {
  title: string;
  sub?: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("mb-s6 flex items-end justify-between gap-s4", className)}>
      <div className="min-w-0">
        <h1 className="font-display text-[26px] font-semibold leading-[1.2] text-ink-primary">{title}</h1>
        {sub && <p className="mt-s1 text-micro text-ink-faint">{sub}</p>}
      </div>
      {actions && <div className="flex flex-none items-center gap-s2">{actions}</div>}
    </header>
  );
}
