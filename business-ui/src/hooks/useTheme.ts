"use client";

import { useContext } from "react";
import { ThemeContext } from "@/providers/ThemeProvider";

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}
