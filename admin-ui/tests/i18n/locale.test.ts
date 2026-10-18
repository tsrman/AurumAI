import { describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, LOCALE_COOKIE, LOCALES, negotiateLocale } from "@/i18n/locale";

describe("negotiateLocale", () => {
  it("supports English and Russian with an English default", () => {
    expect(LOCALES).toEqual(["en", "ru"]);
    expect(DEFAULT_LOCALE).toBe("en");
    expect(LOCALE_COOKIE).toBe("NEXT_LOCALE");
  });

  it("prefers a valid explicit cookie over the browser language", () => {
    expect(negotiateLocale("ru", "en-US,en;q=0.9")).toBe("ru");
    expect(negotiateLocale("en", "ru-RU,ru;q=0.9")).toBe("en");
  });

  it("ignores unsupported or malicious cookie values", () => {
    expect(negotiateLocale("../../etc/passwd", "ru-RU")).toBe("ru");
    expect(negotiateLocale("de", "fr-FR")).toBe("en");
  });

  it("selects Russian for ru* browsers and English otherwise", () => {
    expect(negotiateLocale(undefined, "ru-RU,ru;q=0.9,en;q=0.8")).toBe("ru");
    expect(negotiateLocale(undefined, "de-DE,ru;q=0.5")).toBe("ru");
    expect(negotiateLocale(undefined, "de-DE,fr;q=0.5")).toBe("en");
    expect(negotiateLocale(undefined, null)).toBe("en");
    expect(negotiateLocale(undefined, "en-US,ru;q=0.9")).toBe("en");
  });
});
