import * as React from "react";
import { cn } from "@/lib/utils";

/** Card-framed table used by every admin listing. */
export function Table({ children, className, label }: { children: React.ReactNode; className?: string; label?: string }) {
  return (
    <div className={cn("overflow-x-auto rounded-card border border-aurum-border bg-aurum-surface shadow-card", className)}>
      <table aria-label={label} className="w-full text-left text-sm">
        {children}
      </table>
    </div>
  );
}

export function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return (
    <th scope="col" className={cn("bg-aurum-surface-2 px-4 py-2.5 text-xs font-semibold text-aurum-text-3", className)}>
      {children}
    </th>
  );
}

export function Tr({ children, className, onClick }: { children: React.ReactNode; className?: string; onClick?: () => void }) {
  return (
    <tr
      onClick={onClick}
      className={cn("border-t border-aurum-border-soft align-middle", onClick && "cursor-pointer hover:bg-aurum-surface-2", className)}
    >
      {children}
    </tr>
  );
}

export function Td({ children, className, colSpan }: { children?: React.ReactNode; className?: string; colSpan?: number }) {
  return (
    <td colSpan={colSpan} className={cn("px-4 py-3 text-aurum-text-2", className)}>
      {children}
    </td>
  );
}
