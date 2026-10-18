"use client";

import { useContext } from "react";
import { AppShellContext } from "@/providers/AppShellProvider";

export function useAppShell() {
  const ctx = useContext(AppShellContext);
  if (!ctx) throw new Error("useAppShell must be used inside AppShellProvider");
  return ctx;
}
