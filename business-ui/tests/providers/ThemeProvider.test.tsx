import { render, screen, fireEvent } from "@testing-library/react";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { useTheme } from "@/hooks/useTheme";

function TestComponent() {
  const { theme, toggleTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggleTheme}>toggle theme</button>
      <button onClick={() => setTheme("dark")}>set dark</button>
      <button onClick={() => setTheme("light")}>set light</button>
    </div>
  );
}

beforeEach(() => {
  document.documentElement.classList.remove("dark");
});

test("renders with default light theme", () => {
  render(
    <ThemeProvider>
      <TestComponent />
    </ThemeProvider>
  );
  expect(screen.getByTestId("theme").textContent).toBe("light");
  expect(document.documentElement.classList.contains("dark")).toBe(false);
});

test("toggles theme and applies class to document element", () => {
  render(
    <ThemeProvider>
      <TestComponent />
    </ThemeProvider>
  );
  fireEvent.click(screen.getByText("toggle theme"));
  expect(screen.getByTestId("theme").textContent).toBe("dark");
  expect(document.documentElement.classList.contains("dark")).toBe(true);
});

test("setTheme updates theme and applies class", () => {
  render(
    <ThemeProvider>
      <TestComponent />
    </ThemeProvider>
  );
  fireEvent.click(screen.getByText("set dark"));
  expect(screen.getByTestId("theme").textContent).toBe("dark");
  expect(document.documentElement.classList.contains("dark")).toBe(true);
  fireEvent.click(screen.getByText("set light"));
  expect(screen.getByTestId("theme").textContent).toBe("light");
  expect(document.documentElement.classList.contains("dark")).toBe(false);
});
