"use client";

import { Sun, Moon } from "lucide-react";
import { IconButton } from "@/components/primitives";
import { useTheme } from "@/hooks/useTheme";
import { useTranslations } from "next-intl";

export function ThemeToggle() {
  const t = useTranslations("common.theme");
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";

  return (
    <IconButton
      onClick={toggleTheme}
      aria-label={isDark ? t("enableLight") : t("enableDark")}
      title={isDark ? t("light") : t("dark")}
    >
      {isDark ? (
        <Sun className="h-5 w-5" aria-hidden="true" />
      ) : (
        <Moon className="h-5 w-5" aria-hidden="true" />
      )}
    </IconButton>
  );
}
