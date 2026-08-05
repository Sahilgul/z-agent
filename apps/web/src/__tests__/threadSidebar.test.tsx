import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThreadSidebar, type SidebarThread } from "../components/ThreadSidebar";

const threads: SidebarThread[] = [
  {
    id: "t1",
    persona: "lead",
    repo_scope: "ServerApp/src/auth",
    status: "running",
    steps: 12,
    cost_usd: 3.5,
    budget_usd: 20,
    is_critical: true,
  },
  {
    id: "t2",
    persona: "investigator",
    repo_scope: "ServerApp/src/api",
    status: "idle",
    steps: 4,
    cost_usd: 0.5,
    budget_usd: 10,
  },
  {
    id: "t3",
    persona: "tester",
    repo_scope: null,
    status: "completed",
    steps: 8,
    cost_usd: 1.2,
    budget_usd: 10,
  },
];

describe("ThreadSidebar", () => {
  it("renders all threads", () => {
    render(<ThreadSidebar threads={threads} onSelect={vi.fn()} />);
    expect(screen.getByText("lead")).toBeTruthy();
    expect(screen.getByText("investigator")).toBeTruthy();
    expect(screen.getByText("tester")).toBeTruthy();
  });

  it("shows the thread count", () => {
    render(<ThreadSidebar threads={threads} onSelect={vi.fn()} />);
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("shows empty state when no threads", () => {
    render(<ThreadSidebar threads={[]} onSelect={vi.fn()} />);
    expect(screen.getByText("no threads yet")).toBeTruthy();
  });

  it("calls onSelect with the thread id when clicked", () => {
    const onSelect = vi.fn();
    render(<ThreadSidebar threads={threads} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("lead"));
    expect(onSelect).toHaveBeenCalledWith("t1");
  });

  it("marks the selected thread with aria-current", () => {
    render(<ThreadSidebar threads={threads} selectedId="t2" onSelect={vi.fn()} />);
    const selected = screen.getByText("investigator").closest("button");
    expect(selected).toHaveAttribute("aria-current", "true");
  });

  it("shows the critical path indicator", () => {
    render(<ThreadSidebar threads={threads} onSelect={vi.fn()} />);
    expect(screen.getByText("crit")).toBeTruthy();
  });

  it("shows repo scope when present", () => {
    render(<ThreadSidebar threads={threads} onSelect={vi.fn()} />);
    expect(screen.getByText("ServerApp/src/auth")).toBeTruthy();
  });

  it("shows step count and budget", () => {
    render(<ThreadSidebar threads={threads} onSelect={vi.fn()} />);
    expect(screen.getByText(/12 steps/)).toBeTruthy();
    expect(screen.getByText(/\$3\.50 \/ \$20\.00/)).toBeTruthy();
  });
});
