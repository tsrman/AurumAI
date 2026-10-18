"use client";

import { cn } from "@/lib/utils";

function getInitials(name: string): string {
  return name
    .split(" ")
    .map((part) => part[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

export interface UserCardProps {
  name: string;
  role: string;
  mini?: boolean;
}

export function UserCard({ name, role, mini }: UserCardProps) {
  return (
    <div
      className={cn(
        "flex items-center gap-3 overflow-hidden",
        mini && "justify-center"
      )}
    >
      <span className="sr-only">{name}, {role}</span>
      <div
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-aurum-accent text-sm font-bold text-aurum-accent-ink"
      >
        {getInitials(name)}
      </div>
      {!mini && (
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium text-aurum-text">
            {name}
          </span>
          <span className="truncate text-xs text-aurum-text-3">{role}</span>
        </div>
      )}
    </div>
  );
}
