import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Components resolve text through next-intl. Unit tests render them without a
// provider, so the hooks are backed by the English catalog and assertions check
// the exact English UI text. tests/i18n covers the Russian catalog.
vi.mock("next-intl", async (importOriginal) => {
  const actual = await importOriginal<typeof import("next-intl")>();
  const { default: messages } = await import("../messages/en.json");
  // Cache translators per namespace: next-intl returns stable functions, and
  // hooks list them as effect dependencies.
  const translators = new Map<string, ReturnType<typeof actual.createTranslator>>();
  return {
    ...actual,
    useTranslations: (namespace?: string) => {
      const key = namespace ?? "";
      if (!translators.has(key)) {
        translators.set(key, actual.createTranslator({ locale: "en", messages, namespace: namespace as never }));
      }
      return translators.get(key)!;
    },
    useLocale: () => "en",
    useFormatter: () => actual.createFormatter({ locale: "en", timeZone: "UTC" }),
  };
});
