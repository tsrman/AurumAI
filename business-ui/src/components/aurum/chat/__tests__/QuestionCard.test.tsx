import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QuestionCard } from "../QuestionCard";
import type { QuestionResponse } from "@/types/chat";

const pendingQuestion: QuestionResponse = {
  questionId: "question-1",
  turnId: "turn-1",
  text: "Specify the city",
  options: [],
  status: "pending",
  version: 1,
  expiresAt: "2099-01-01T00:00:00Z",
  createdAt: "2026-09-06T10:00:00Z",
  phase: "ordinary",
  nodeKey: null,
  agentName: "Analyst",
  answer: null,
  answeredAt: null,
};

describe("QuestionCard", () => {
  it("submits on an HTTP origin where crypto.randomUUID is unavailable", async () => {
    const onAnswer = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.set([
          0x10, 0x32, 0x54, 0x76, 0x98, 0xba, 0xdc, 0xfe,
          0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef,
        ]);
        return bytes;
      },
    });

    try {
      render(<QuestionCard question={pendingQuestion} onAnswer={onAnswer} />);
      fireEvent.change(screen.getByRole("textbox", { name: "Answer" }), {
        target: { value: "Berlin" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Answer" }));

      await waitFor(() => {
        expect(onAnswer).toHaveBeenCalledWith(
          { text: "Berlin" },
          "10325476-98ba-4cfe-8123-456789abcdef"
        );
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("submits a trimmed free-text answer", async () => {
    const onAnswer = vi.fn().mockResolvedValue(undefined);
    render(<QuestionCard question={pendingQuestion} onAnswer={onAnswer} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Answer" }), {
      target: { value: "  Berlin  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    await waitFor(() => {
      expect(onAnswer).toHaveBeenCalledWith(
        { text: "Berlin" },
        expect.any(String)
      );
    });
  });

  it("submits the selected option id", async () => {
    const onAnswer = vi.fn().mockResolvedValue(undefined);
    render(
      <QuestionCard
        question={{
          ...pendingQuestion,
          text: "Choose a priority",
          options: [
            { id: "high", label: "High" },
            { id: "normal", label: "Normal" },
          ],
        }}
        onAnswer={onAnswer}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "High" }));

    await waitFor(() => {
      expect(onAnswer).toHaveBeenCalledWith(
        { optionId: "high" },
        expect.any(String)
      );
    });
  });

  it("keeps the answer and reuses the idempotency key after a network failure", async () => {
    const onAnswer = vi
      .fn()
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValueOnce(undefined);
    render(<QuestionCard question={pendingQuestion} onAnswer={onAnswer} />);

    const textbox = screen.getByRole("textbox", { name: "Answer" });
    fireEvent.change(textbox, { target: { value: "Berlin" } });
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
    expect(textbox).toHaveValue("Berlin");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
    expect(onAnswer.mock.calls[1][0]).toEqual({ text: "Berlin" });
    expect(onAnswer.mock.calls[1][1]).toBe(onAnswer.mock.calls[0][1]);
  });

  it("renders an answered question as read-only with the submitted label", () => {
    render(
      <QuestionCard
        question={{
          ...pendingQuestion,
          options: [{ id: "high", label: "High" }],
          status: "answered",
          version: 2,
          answer: { optionId: "high" },
          answeredAt: "2026-09-06T10:05:00Z",
        }}
        onAnswer={vi.fn()}
      />
    );

    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText(/Answered/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Answer" })).not.toBeInTheDocument();
  });

  it.each([
    ["expired", "The answer deadline has passed"],
    ["cancelled", "Question cancelled"],
  ] as const)("renders %s as an inactive state", (status, label) => {
    render(
      <QuestionCard
        question={{ ...pendingQuestion, status, version: 2 }}
        onAnswer={vi.fn()}
      />
    );

    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("lets the server reject a pending question after the displayed deadline", async () => {
    const onAnswer = vi.fn().mockRejectedValue(new Error("The answer deadline has passed"));
    render(
      <QuestionCard
        question={{ ...pendingQuestion, expiresAt: "2000-01-01T00:00:00Z" }}
        onAnswer={onAnswer}
      />
    );

    fireEvent.change(screen.getByRole("textbox", { name: "Answer" }), {
      target: { value: "Berlin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The answer deadline has passed");
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });
});
