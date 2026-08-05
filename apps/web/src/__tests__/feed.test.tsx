import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Feed, type FeedItem } from "../components/feed/Feed";

const items: FeedItem[] = [
  {
    key: "1",
    kind: "command",
    title: "$ git status",
    text: "On branch main\nnothing to commit",
    threadId: "t1",
    ok: true,
    live: false,
    role: null,
  },
  {
    key: "2",
    kind: "file_edit",
    title: "Edit src/auth.ts",
    text: "- old line\n+ new line",
    threadId: "t1",
    ok: true,
    live: false,
    role: null,
    filePath: "src/auth.ts",
    diff: "- old line\n+ new line",
  },
  {
    key: "3",
    kind: "message",
    title: "Done",
    text: "I've fixed the bug.",
    threadId: "t1",
    ok: null,
    live: false,
    role: "agent",
  },
  {
    key: "4",
    kind: "thinking",
    title: "thinking…",
    text: "Let me consider the approach…",
    threadId: "t1",
    ok: null,
    live: true,
    role: null,
  },
];

describe("Feed", () => {
  it("renders all items", () => {
    render(<Feed items={items} />);
    expect(screen.getByText("$ git status")).toBeTruthy();
    expect(screen.getByText("Edit src/auth.ts")).toBeTruthy();
    expect(screen.getByText("I've fixed the bug.")).toBeTruthy();
  });

  it("shows empty state when no items", () => {
    render(<Feed items={[]} />);
    expect(screen.getByText(/no trace yet/)).toBeTruthy();
  });

  it("filters by threadId", () => {
    render(<Feed items={items} threadFilter="t1" />);
    expect(screen.getByText("$ git status")).toBeTruthy();
  });

  it("filters out items from other threads", () => {
    const mixed: FeedItem[] = [
      { ...items[0], threadId: "t2" },
      { ...items[1], threadId: "t1" },
    ];
    render(<Feed items={mixed} threadFilter="t1" />);
    expect(screen.queryByText("$ git status")).toBeNull();
    expect(screen.getByText("Edit src/auth.ts")).toBeTruthy();
  });

  it("renders the prompt as a user bubble when provided", () => {
    render(<Feed items={[]} prompt="fix the auth bug" />);
    expect(screen.getByText("fix the auth bug")).toBeTruthy();
  });

  it("opens the viewer when a preview row is clicked", () => {
    render(<Feed items={items} />);
    expect(screen.queryByTestId("viewer")).toBeNull();
    fireEvent.click(screen.getByText("Edit src/auth.ts"));
    expect(screen.getByTestId("viewer")).toBeTruthy();
  });

  it("closes the viewer on Escape", () => {
    render(<Feed items={items} />);
    fireEvent.click(screen.getByText("Edit src/auth.ts"));
    expect(screen.getByTestId("viewer")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("viewer")).toBeNull();
  });

  it("closes the viewer when the backdrop is clicked", () => {
    render(<Feed items={items} />);
    fireEvent.click(screen.getByText("Edit src/auth.ts"));
    const backdrop = screen.getByTestId("viewer");
    fireEvent.click(backdrop);
    expect(screen.queryByTestId("viewer")).toBeNull();
  });

  it("shows failed indicator when ok is false", () => {
    const failed: FeedItem[] = [
      { ...items[0], ok: false },
    ];
    render(<Feed items={failed} />);
    expect(screen.getByText("failed")).toBeTruthy();
  });

  it("renders message bubbles with role alignment", () => {
    render(<Feed items={[items[2]]} />);
    const bubble = screen.getByText("I've fixed the bug.").closest("div[data-role]");
    expect(bubble?.getAttribute("data-role")).toBe("agent");
  });
});
