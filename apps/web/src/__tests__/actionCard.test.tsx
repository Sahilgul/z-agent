import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ActionCard } from "../components/ActionCard";

describe("ActionCard", () => {
  it("renders legal moves as buttons", () => {
    render(
      <ActionCard stage="awaiting_user" actions={["approve_plan", "reject_plan"]} onFire={() => {}} />,
    );
    expect(screen.getByText("Approve plan")).toBeInTheDocument();
    expect(screen.getByText("Reject plan")).toBeInTheDocument();
  });

  it("irreversible intents are two-tap: first tap asks, second fires confirmed", () => {
    const onFire = vi.fn();
    render(<ActionCard stage="pr_ready" actions={["merge_pr"]} onFire={onFire} />);
    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    expect(onFire).not.toHaveBeenCalled();
    expect(screen.getByRole("button")).toHaveTextContent("confirm");
    fireEvent.click(screen.getByRole("button"));
    expect(onFire).toHaveBeenCalledWith("merge_pr", true);
  });

  it("while the agent works only Stop renders", () => {
    render(
      <ActionCard stage="developing" actions={["stop_run", "create_pr", "merge_pr"]} onFire={() => {}} />,
    );
    expect(screen.getByText("Stop")).toBeInTheDocument();
    expect(screen.queryByText("Open PR")).not.toBeInTheDocument();
    expect(screen.queryByText("Merge PR")).not.toBeInTheDocument();
  });
});
