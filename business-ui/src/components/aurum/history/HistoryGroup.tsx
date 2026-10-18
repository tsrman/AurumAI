"use client";

import * as React from "react";

export interface HistoryGroupProps {
  date: string;
  children: React.ReactNode;
}

export function HistoryGroup({ date, children }: HistoryGroupProps) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="px-1 text-xs font-bold tracking-wider text-aurum-text-3 uppercase">
        {date}
      </h2>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  );
}
