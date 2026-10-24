"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const tabClass = (active: boolean) =>
  cn(
    "-mb-px inline-flex items-center gap-2 border-b-2 px-1 pb-3 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-aurum-accent/50",
    active
      ? "border-aurum-accent text-aurum-text"
      : "border-transparent text-aurum-text-3 hover:text-aurum-text"
  );

export interface LinkTab {
  href: string;
  label: string;
}

/** Route-backed section tabs (Users | Groups). */
export function LinkTabs({ tabs, label }: { tabs: LinkTab[]; label: string }) {
  const pathname = usePathname();
  return (
    <nav aria-label={label} className="flex gap-6 overflow-x-auto">
      {tabs.map((tab) => {
        const active = pathname === tab.href || pathname.startsWith(`${tab.href}/`);
        return (
          <Link key={tab.href} href={tab.href} aria-current={active ? "page" : undefined} className={tabClass(active)}>
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}

export interface ButtonTab<T extends string> {
  value: T;
  label: string;
  disabled?: boolean;
}

/** State-backed tabs for views inside one page or dialog. */
export function ButtonTabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  className,
}: {
  tabs: ButtonTab<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  return (
    <div role="tablist" aria-label={label} className={cn("flex gap-6 overflow-x-auto", className)}>
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={value === tab.value}
          disabled={tab.disabled}
          onClick={() => onChange(tab.value)}
          className={cn(tabClass(value === tab.value), "disabled:opacity-50")}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}
