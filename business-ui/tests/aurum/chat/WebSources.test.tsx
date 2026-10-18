import { render, screen } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { SourceDrawer } from "@/components/aurum/chat/SourceDrawer";
import { sourcesForMessage } from "@/lib/web-sources-api";
import { AppShellProvider } from "@/providers/AppShellProvider";
import type { Source } from "@/types/aurum";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: false, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

const webSource: Source = {
  kind: "web",
  index: 1,
  sourceId: "source-1",
  url: "https://example.org/report",
  title: '<img src=x onerror="alert(1)">Report',
  snippet: "Public report",
  retrievedAt: "2026-09-07T00:00:00Z",
  publishedAt: "2026-09-06T00:00:00Z",
  truncated: true,
};

describe("web source presentation", () => {
  it("renders title as text and opens the URL without a referrer", () => {
    render(
      React.createElement(
        AppShellProvider,
        { initialState: { drawerSource: webSource } },
        React.createElement(SourceDrawer)
      )
    );

    expect(screen.getByText(webSource.title)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("Source truncated")).toBeInTheDocument();
    expect(screen.getByText("Published: 9/6/2026")).toBeInTheDocument();
    expect(screen.getByText("Retrieved: 9/7/2026")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open source" });
    expect(link).toHaveAttribute("href", "https://example.org/report");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("referrerpolicy", "no-referrer");
  });

  it("shows an unsupported citation as unverified", () => {
    const unsupported: Source = {
      kind: "unsupported",
      index: 1,
      sourceId: "unknown-source",
    };
    render(
      React.createElement(
        AppShellProvider,
        { initialState: { drawerSource: unsupported } },
        React.createElement(SourceDrawer)
      )
    );

    expect(screen.getByText("Unverified source")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("server source filtering", () => {
  it("keeps accepted sources, marks unsupported citations and drops foreign data", () => {
    const sources = sourcesForMessage(
      ["source-1", "foreign-source"],
      ["unknown-source"],
      [
        {
          sourceId: "source-1",
          kind: "read",
          requestedUrl: "https://example.org/report",
          finalUrl: "https://example.org/report",
          title: "Report",
          contentType: "text/html",
          snippet: "Public report",
          publishedAt: null,
          retrievedAt: "2026-09-07T00:00:00Z",
          truncated: false,
          pageCount: null,
        },
        {
          sourceId: "not-claimed",
          kind: "read",
          requestedUrl: "https://attacker.example/",
          finalUrl: "https://attacker.example/",
          title: "Foreign",
          contentType: "text/html",
          snippet: "Must not render",
          publishedAt: null,
          retrievedAt: "2026-09-07T00:00:00Z",
          truncated: false,
          pageCount: null,
        },
      ]
    );

    expect(sources).toHaveLength(2);
    expect(sources[0]).toMatchObject({
      kind: "web",
      sourceId: "source-1",
      url: "https://example.org/report",
    });
    expect(sources[1]).toEqual({
      kind: "unsupported",
      index: 2,
      sourceId: "unknown-source",
    });
    expect(JSON.stringify(sources)).not.toContain("attacker.example");
    expect(JSON.stringify(sources)).not.toContain("foreign-source");
  });

  it("rejects a non-http URL even when returned by the API", () => {
    const sources = sourcesForMessage(["source-1"], [], [
      {
        sourceId: "source-1",
        kind: "read",
        requestedUrl: null,
        finalUrl: "javascript:alert(1)",
        title: "Unsafe",
        contentType: "text/html",
        snippet: null,
        publishedAt: null,
        retrievedAt: "2026-09-07T00:00:00Z",
        truncated: false,
        pageCount: null,
      },
    ]);

    expect(sources).toEqual([]);
  });
});
