export type Theme = "light" | "dark";

export const STORAGE_KEY = "aurum-theme";

export function getInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    // Ignore storage access errors and fall through to system preference.
  }

  try {
    if (matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
  } catch {
    // Ignore matchMedia errors.
  }

  return "light";
}

export function setStoredTheme(theme: Theme) {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Ignore storage write errors.
  }
}
