"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import {
  BarChart3,
  BookOpen,
  Bot,
  Cpu,
  GitBranch,
  Info,
  LogOut,
  MessageSquare,
  PanelLeft,
  ShieldCheck,
  Users,
  Wrench,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { IconButton } from "@/components/primitives";
import { ThemeToggle } from "@/components/aurum/ThemeToggle";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/AuthProvider";
import { NavItem } from "./NavItem";
import { UserCard } from "./UserCard";

const COLLAPSED_KEY = "aurum-admin-sidebar-collapsed";

type NavKey = "users" | "agents" | "skills" | "workflows" | "knowledge" | "models" | "analytics";

interface NavEntry {
  key: NavKey;
  href: string;
  icon: React.ReactNode;
  /** Other routes that belong to the same section. */
  matches?: string[];
}

const SECTIONS: { key: "people" | "workspace" | "monitoring"; items: NavEntry[] }[] = [
  {
    key: "people",
    items: [
      { key: "users", href: "/admin/users", icon: <Users className="h-5 w-5" aria-hidden="true" />, matches: ["/admin/groups"] },
    ],
  },
  {
    key: "workspace",
    items: [
      { key: "agents", href: "/admin/agents", icon: <Bot className="h-5 w-5" aria-hidden="true" /> },
      { key: "skills", href: "/admin/skills", icon: <Wrench className="h-5 w-5" aria-hidden="true" /> },
      { key: "workflows", href: "/admin/workflows", icon: <GitBranch className="h-5 w-5" aria-hidden="true" /> },
      { key: "knowledge", href: "/admin/knowledge", icon: <BookOpen className="h-5 w-5" aria-hidden="true" /> },
      { key: "models", href: "/admin/models", icon: <Cpu className="h-5 w-5" aria-hidden="true" /> },
    ],
  },
  {
    key: "monitoring",
    items: [
      { key: "analytics", href: "/admin/analytics", icon: <BarChart3 className="h-5 w-5" aria-hidden="true" /> },
    ],
  },
];

function isActive(pathname: string, entry: NavEntry): boolean {
  return [entry.href, ...(entry.matches ?? [])].some(
    (href) => pathname === href || pathname.startsWith(`${href}/`)
  );
}

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** Admin counterpart of business-ui's Sidebar: same frame, brand and bottom
 * controls, with the admin sections instead of agents and history. */
export function AdminSidebar() {
  const t = useTranslations("shell");
  const roles = useTranslations("people.roles");
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const [mini, setMini] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- browser storage is only readable after hydration
    setMini(readCollapsed());
  }, []);

  const toggle = () => {
    setMini((current) => {
      try {
        window.localStorage.setItem(COLLAPSED_KEY, current ? "0" : "1");
      } catch {
        // Collapsing still works for this page view without storage.
      }
      return !current;
    });
  };

  return (
    <aside
      aria-label={t("panel")}
      className={cn(
        "flex h-full shrink-0 flex-col border-r border-aurum-border bg-aurum-surface transition-all",
        mini ? "w-[68px]" : "w-[264px]"
      )}
    >
      <div className={cn("flex items-center gap-2 border-b border-aurum-border p-4", mini && "justify-center px-2")}>
        <ShieldCheck className="h-6 w-6 shrink-0 text-aurum-accent" aria-hidden="true" />
        {!mini && (
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="text-lg font-bold tracking-wide text-aurum-text">AurumAI</span>
            <span className="truncate text-xs font-semibold uppercase tracking-wider text-aurum-text-3">{t("brandSuffix")}</span>
          </span>
        )}
      </div>

      <div className={cn("p-4 pb-2", mini && "px-2")}>
        <NavItem
          external
          href={getRuntimeConfig().appUrl}
          icon={<MessageSquare className="h-5 w-5" aria-hidden="true" />}
          label={t("backToChat")}
          mini={mini}
        />
      </div>

      <nav aria-label={t("navigation")} className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-2">
        {SECTIONS.map((section) => (
          <div key={section.key} className={cn("flex flex-col gap-1", mini && "-mx-2")}>
            {!mini && (
              <div className="mb-1 px-3 text-xs font-bold tracking-wider text-aurum-text-3">
                {t(`sections.${section.key}`)}
              </div>
            )}
            {section.items.map((item) => (
              <NavItem
                key={item.key}
                href={item.href}
                icon={item.icon}
                label={t(`items.${item.key}`)}
                active={isActive(pathname, item)}
                mini={mini}
              />
            ))}
          </div>
        ))}
      </nav>

      <div className={cn("border-t border-aurum-border p-4", mini && "px-2")}>
        <div className={cn("mb-3", mini && "-mx-2")}>
          <NavItem
            href="/about"
            icon={<Info className="h-5 w-5" aria-hidden="true" />}
            label={t("about")}
            active={pathname === "/about"}
            mini={mini}
          />
        </div>
        <div className={cn("mb-3 flex items-center gap-2", mini && "flex-col justify-center")}>
          <IconButton onClick={toggle} aria-label={mini ? t("expand") : t("collapse")} title={mini ? t("expand") : t("collapse")}>
            <PanelLeft className={cn("h-5 w-5 transition-transform", mini && "rotate-180")} aria-hidden="true" />
          </IconButton>
          <ThemeToggle />
          {!mini ? <LanguageSwitcher /> : null}
          <IconButton onClick={logout} aria-label={t("logout")} title={t("logout")}>
            <LogOut className="h-5 w-5" aria-hidden="true" />
          </IconButton>
        </div>
        <UserCard name={user?.fullName || user?.email || t("userFallback")} role={user?.role === "org_admin" ? roles("org_admin") : user?.role === "member" ? roles("member") : user?.role || ""} mini={mini} />
      </div>
    </aside>
  );
}
