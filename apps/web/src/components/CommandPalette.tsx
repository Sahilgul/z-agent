import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  BookOpenIcon,
  CircleDollarSignIcon,
  FolderGit2Icon,
  InboxIcon,
  LightbulbIcon,
  LogOutIcon,
  PlusIcon,
  RadarIcon,
  UsersIcon,
} from "lucide-react";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import { useSession } from "../stores/session";
import { useAppTranslation } from "@/i18n";

/** Global ⌘K / Ctrl+K command palette — the navigation affordance the
 *  review flagged as missing. Wires the already-installed cmdk + command
 *  primitive that was never mounted. Opens on keydown; closes on selection
 *  or escape (built into the Dialog primitive). */
const NAV = [
  { to: "/", labelKey: "nav.sessions", icon: InboxIcon, hint: "⌘1" },
  { to: "/knowledge", labelKey: "nav.knowledge", icon: BookOpenIcon, hint: "⌘2" },
  { to: "/ideas", labelKey: "nav.ideas", icon: LightbulbIcon, hint: "⌘3" },
  { to: "/patrol", labelKey: "nav.patrol", icon: RadarIcon, hint: "⌘4" },
  { to: "/costs", labelKey: "nav.costs", icon: CircleDollarSignIcon, hint: "⌘5" },
  { to: "/repos", labelKey: "nav.repos", icon: FolderGit2Icon, hint: "⌘6" },
  { to: "/team", labelKey: "nav.team", icon: UsersIcon, hint: "⌘7" },
] as const;

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { t } = useAppTranslation();
  const logout = useSession((s) => s.logout);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (to: string) => {
    navigate(to);
    setOpen(false);
  };

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title={t("cmdk.title")} description={t("cmdk.desc")}>
      <CommandInput placeholder={t("cmdk.placeholder")} />
      <CommandList>
        <CommandEmpty>{t("cmdk.empty")}</CommandEmpty>
        <CommandGroup heading={t("cmdk.navigate")}>
          {NAV.map((n) => (
            <CommandItem key={n.to} onSelect={() => go(n.to)} value={`${t(n.labelKey)} ${n.to}`}>
              <n.icon className="size-4" aria-hidden="true" />
              <span>{t(n.labelKey)}</span>
              <CommandShortcut>{n.hint}</CommandShortcut>
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading={t("cmdk.actions")}>
          <CommandItem
            onSelect={() => {
              go("/");
            }}
            value={`${t("cmdk.newRun")} inbox`}
          >
            <PlusIcon className="size-4" aria-hidden="true" />
            <span>{t("cmdk.newRun")}</span>
            <CommandShortcut>⌘N</CommandShortcut>
          </CommandItem>
          <CommandItem
            onSelect={() => {
              void logout();
              setOpen(false);
            }}
            value={`${t("common.signOut")} logout`}
          >
            <LogOutIcon className="size-4" aria-hidden="true" />
            <span>{t("common.signOut")}</span>
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
