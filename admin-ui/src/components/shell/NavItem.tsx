"use client";

import * as React from "react";
import Link from "next/link";
import { cn } from "@/lib/utils";

export interface NavItemProps {
  icon: React.ReactNode;
  label: string;
  href: string;
  active?: boolean;
  mini?: boolean;
  /** Cross-origin destinations render a plain anchor: there is no client route to prefetch. */
  external?: boolean;
}

/** Same item styling as business-ui's sidebar. */
export function NavItem({ icon, label, href, active, mini, external }: NavItemProps) {
  const className = cn(
    "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
    active
      ? "bg-aurum-accent-soft font-semibold text-aurum-text"
      : "text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text",
    mini && "justify-center px-2"
  );
  const content = (
    <>
      <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center">{icon}</span>
      {!mini && <span className="truncate text-left">{label}</span>}
    </>
  );
  const shared = {
    "aria-current": active ? ("page" as const) : undefined,
    "aria-label": mini ? label : undefined,
    title: mini ? label : undefined,
    className,
  };
  return external ? (
    <a href={href} {...shared}>{content}</a>
  ) : (
    <Link href={href} {...shared}>{content}</Link>
  );
}
