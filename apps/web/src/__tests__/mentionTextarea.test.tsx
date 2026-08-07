import { describe, it, expect, vi, beforeEach } from "vitest";
import { useState } from "react";
import { screen, fireEvent } from "@testing-library/react";
import { renderScreen } from "./render";
import { MentionTextarea } from "../components/MentionTextarea";

const get = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a) },
}));

const repos = [
  { id: 1, name: "ServerApp", integration_branch: "main" },
  { id: 2, name: "ClientApp", integration_branch: "develop" },
  { id: 3, name: "Billing-Engine", integration_branch: "pg-main" },
];

/** A stateful wrapper so the parent owns the value (mirrors SessionsScreen).
 * The MentionTextarea is controlled — the parent holds the text state. */
function Harness({ onKeyDown, initial = "" }: {
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  initial?: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <MentionTextarea
      id="session-composer"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={onKeyDown}
      placeholder="describe the task"
    />
  );
}

/** Type into the textarea the way a user would: set the value AND park the
 * caret at the end (jsdom's fireEvent.change leaves selectionStart at 0, so
 * the token-at-caret detector would never see a token without this). */
function type(textarea: HTMLTextAreaElement, value: string) {
  fireEvent.change(textarea, { target: { value } });
  textarea.setSelectionRange(value.length, value.length);
}

function setup(onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void, initial = "") {
  renderScreen(<Harness onKeyDown={onKeyDown} initial={initial} />);
  const textarea = screen.getByPlaceholderText("describe the task") as HTMLTextAreaElement;
  if (initial) {
    // Park the caret at the end (where the user's would be after typing),
    // then fire a keyup so the token-at-caret detector re-syncs — the
    // mount-time useEffect runs before we can set the selection, so the
    // initial pass sees the caret at 0 and finds no token.
    textarea.setSelectionRange(initial.length, initial.length);
    fireEvent.keyUp(textarea, { key: initial.slice(-1) });
  }
  return { textarea };
}

describe("MentionTextarea", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue(repos);
  });

  it("renders a textarea that accepts input", () => {
    const { textarea } = setup();
    type(textarea, "hello");
    expect(textarea.value).toBe("hello");
  });

  it("opens the dropdown on @ with the full fleet", async () => {
    setup(undefined, "@");
    // The dropdown renders after the @ is typed and the query resolves.
    expect(await screen.findByRole("listbox")).toBeTruthy();
    expect(screen.getByText("ServerApp")).toBeTruthy();
    expect(screen.getByText("ClientApp")).toBeTruthy();
    expect(screen.getByText("Billing-Engine")).toBeTruthy();
  });

  it("substring-filters the dropdown as the user types", async () => {
    setup(undefined, "@bill");
    const listbox = await screen.findByRole("listbox");
    // Only Billing-Engine matches "bill" (case-insensitive).
    expect(listbox.textContent).toContain("Billing-Engine");
    expect(listbox.textContent).not.toContain("ServerApp");
    expect(listbox.textContent).not.toContain("ClientApp");
  });

  it("Enter inserts the backtick-wrapped @mention and a trailing space", async () => {
    const { textarea } = setup(undefined, "@serv");
    await screen.findByRole("listbox");
    fireEvent.keyDown(textarea, { key: "Enter" });
    // The composer wraps the pick as `@ServerApp` so the backend parser
    // recognizes it as a scope directive (bare @word is prose, not a mount).
    expect(textarea.value).toBe("`@ServerApp` ");
  });

  it("Enter submits when the dropdown is closed (no @token)", () => {
    const onKeyDown = vi.fn((e) => {
      if (e.key === "Enter") e.preventDefault();
    });
    const { textarea } = setup(onKeyDown, "fix the bug");
    textarea.setSelectionRange("fix the bug".length, "fix the bug".length);
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    // The parent's onKeyDown (the submit handler) runs unchanged.
    expect(onKeyDown).toHaveBeenCalledTimes(1);
  });

  it("Esc closes the dropdown without submitting", async () => {
    const onKeyDown = vi.fn();
    const { textarea } = setup(onKeyDown, "@serv");
    await screen.findByRole("listbox");
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    // Esc does NOT fall through to the parent's submit handler.
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it("ArrowDown/Up navigate the dropdown", async () => {
    const { textarea } = setup(undefined, "@");
    await screen.findByRole("listbox");
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
    fireEvent.keyDown(textarea, { key: "ArrowUp" });
    expect(options[0]).toHaveAttribute("aria-selected", "true");
  });

  it("ArrowDown keyUP must not snap the highlight back to the top", async () => {
    // Real browsers fire keyup after keydown; resync() runs on keyup and used
    // to reset active=0 unconditionally — the highlight moved on keydown and
    // snapped back on keyup, so ArrowDown appeared to do nothing.
    const { textarea } = setup(undefined, "@");
    await screen.findByRole("listbox");
    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    fireEvent.keyUp(textarea, { key: "ArrowDown" });
    const options = screen.getAllByRole("option");
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
  });

  it("click selects the repo", async () => {
    const { textarea } = setup(undefined, "@client");
    await screen.findByRole("listbox");
    fireEvent.click(screen.getByText("ClientApp"));
    expect(textarea.value).toBe("`@ClientApp` ");
  });
});
