import { render, screen } from "@testing-library/react";
import { KnowledgeGrid } from "@/components/aurum/knowledge/KnowledgeGrid";
import type { KnowledgeBase } from "@/types/aurum";

const serverKnowledgeBases: KnowledgeBase[] = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Server runbooks",
    badge: "SE",
    badgeColor: "amber",
    description: "Persistent data",
    canWrite: true,
    docCount: 0,
    size: "—",
    updated: "01.09.2026",
    status: "indexed",
    docs: [],
  },
];

test("renders grid with a card for each knowledge base", () => {
  render(<KnowledgeGrid knowledgeBases={serverKnowledgeBases} />);

  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(serverKnowledgeBases.length);

  for (const kb of serverKnowledgeBases) {
    expect(screen.getByText(kb.name)).toBeInTheDocument();
  }
});
