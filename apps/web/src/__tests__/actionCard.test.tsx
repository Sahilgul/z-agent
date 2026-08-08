import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ActionCard } from "../components/ActionCard";

// Fixtures mirror backend/app/services/runs.py ACTIONS_BY_STAGE — no test
// may pin a contract the backend never produces (fixture honesty rule).
// awaiting_user: [review_plan, approve_plan, reject_plan]; verifying:
// [review_evidence, create_pr]; developing/queued: []. stop_run/abandon_run
// are NEVER advertised — the UI hardcodes them (W-B1).

describe("ActionCard", () => {
  it("renders legal moves as buttons (awaiting_user, backend-real actions)", () => {
    render(
      <ActionCard
        stage="awaiting_user"
        actions={["review_plan", "approve_plan", "reject_plan"]}
        onFire={() => {}}
      />,
    );
    expect(screen.getByText("approve plan")).toBeInTheDocument();
    expect(screen.getByText("reject plan")).toBeInTheDocument();
  });

  it("hardcodes Stop on every non-terminal stage even when the server doesn't advertise it (W-B1)", () => {
    render(<ActionCard stage="developing" actions={[]} onFire={() => {}} />);
    const stop = screen.getByText("stop");
    expect(stop).toBeInTheDocument();
    // Fires unconfirmed — stopping is safe and reversible.
    const onFire = vi.fn();
    render(<ActionCard stage="developing" actions={[]} onFire={onFire} />);
    fireEvent.click(screen.getAllByText("stop")[1]);
    expect(onFire).toHaveBeenCalledWith("stop_run", false);
  });

  it("abandon is two-tap: first tap arms, second fires confirmed", () => {
    const onFire = vi.fn();
    render(<ActionCard stage="developing" actions={[]} onFire={onFire} />);
    const btn = screen.getByText("abandon");
    fireEvent.click(btn);
    expect(onFire).not.toHaveBeenCalled();
    expect(screen.getByText("confirm abandon?")).toBeInTheDocument();
    fireEvent.click(screen.getByText("confirm abandon?"));
    expect(onFire).toHaveBeenCalledWith("abandon_run", true);
  });

  it("renders review actions at verifying (W-H3 — it is the human's turn, not the agent's)", () => {
    render(
      <ActionCard stage="verifying" actions={["review_evidence", "create_pr"]} onFire={() => {}} />,
    );
    expect(screen.getByText("evidence")).toBeInTheDocument();
    expect(screen.getByText("create PR")).toBeInTheDocument();
  });

  it("unmounts on terminal stages", () => {
    const { container } = render(
      <ActionCard stage="completed" actions={[]} onFire={() => {}} />,
    );
    expect(container.querySelector("[data-testid='action-strip']")).toBeNull();
  });

  it("disables buttons while an intent is in flight (W3-M6)", () => {
    const onFire = vi.fn();
    render(<ActionCard stage="developing" actions={[]} onFire={onFire} />);
    const stop = screen.getByText("stop");
    fireEvent.click(stop);
    expect(onFire).toHaveBeenCalledTimes(1);
    fireEvent.click(stop);
    fireEvent.click(screen.getByText("abandon"));
    expect(onFire).toHaveBeenCalledTimes(1); // busy: second stop + abandon swallowed
  });
});
