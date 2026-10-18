import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { NavItem } from "@/components/aurum/NavItem";

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

const TestIcon = () => (
  <span data-testid="icon" aria-hidden="true">
    icon
  </span>
);

test("renders as button when no href", () => {
  render(<NavItem icon={<TestIcon />} label="Dashboard" />);

  expect(screen.getByRole("button", { name: "Dashboard" })).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});

test("renders as link when href provided", () => {
  render(<NavItem icon={<TestIcon />} label="Settings" href="/settings" />);

  const link = screen.getByRole("link", { name: "Settings" });
  expect(link).toBeInTheDocument();
  expect(link).toHaveAttribute("href", "/settings");
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("active state has aria-current=page for links", () => {
  render(
    <NavItem icon={<TestIcon />} label="Home" href="/" active />
  );

  expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute(
    "aria-current",
    "page"
  );
});

test("mini mode has aria-label", () => {
  render(<NavItem icon={<TestIcon />} label="Profile" mini />);

  expect(screen.getByRole("button", { name: "Profile" })).toHaveAttribute(
    "aria-label",
    "Profile"
  );
});
