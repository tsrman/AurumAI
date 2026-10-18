"use client";

import * as React from "react";
import Link from "next/link";
import { cn } from "@/lib/utils";

export interface NavItemProps {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  badge?: React.ReactNode;
  mini?: boolean;
  href?: string;
  onClick?: () => void;
}

export function NavItem({
  icon,
  label,
  active,
  badge,
  mini,
  href,
  onClick,
}: NavItemProps) {
  const className = cn(
    "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
    active
      ? "bg-aurum-accent-soft font-semibold text-aurum-text"
      : "text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text",
    mini && "justify-center px-2"
  );

  const content = (
    <>
      <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center">
        {icon}
      </span>
      {!mini && <span className="truncate text-left">{label}</span>}
      {!mini && badge ? (
        <span className="ml-auto shrink-0">{badge}</span>
      ) : null}
    </>
  );

  if (href) {
    return (
      <Link
        href={href}
        onClick={onClick}
        aria-current={active ? "page" : undefined}
        aria-label={mini ? label : undefined}
        className={className}
      >
        {content}
      </Link>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={mini ? label : undefined}
      className={className}
    >
      {content}
    </button>
  );
}
