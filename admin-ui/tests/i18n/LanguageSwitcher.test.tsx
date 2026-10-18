import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

beforeEach(() => {
  refresh.mockReset();
  document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
});

test("offers both languages and marks the active one", () => {
  render(<LanguageSwitcher />);
  const select = screen.getByLabelText("Language") as HTMLSelectElement;
  expect(select.value).toBe("en");
  expect([...select.options].map((option) => option.value)).toEqual(["en", "ru"]);
  expect(screen.getByRole("option", { name: "English" })).toBeInTheDocument();
  // The Russian autonym, escaped so test sources stay English-only.
  expect(screen.getByRole("option", { name: "\u0420\u0443\u0441\u0441\u043a\u0438\u0439" })).toBeInTheDocument();
});

test("persists the chosen language in the locale cookie and re-renders", () => {
  render(<LanguageSwitcher />);
  fireEvent.change(screen.getByLabelText("Language"), { target: { value: "ru" } });
  expect(document.cookie).toContain("NEXT_LOCALE=ru");
  expect(refresh).toHaveBeenCalledTimes(1);
});
