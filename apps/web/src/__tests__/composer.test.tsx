import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Composer, type ComposerPayload } from "../components/Composer";

describe("Composer", () => {
  it("renders the two-row layout", () => {
    render(<Composer onSubmit={vi.fn()} />);
    expect(screen.getByTestId("composer")).toBeTruthy();
    expect(screen.getByLabelText("prompt")).toBeTruthy();
    expect(screen.getByLabelText("mode")).toBeTruthy();
    expect(screen.getByLabelText("model")).toBeTruthy();
    expect(screen.getByLabelText("budget")).toBeTruthy();
    expect(screen.getByText("send")).toBeTruthy();
  });

  it("disables send when prompt is empty", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const send = screen.getByText("send");
    expect(send).toBeDisabled();
  });

  it("enables send when prompt has text", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const textarea = screen.getByLabelText("prompt");
    fireEvent.change(textarea, { target: { value: "fix the bug" } });
    expect(screen.getByText("send")).not.toBeDisabled();
  });

  it("submits the payload with mode/model/budget", () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} defaultMode="goal" defaultModel="qwen-foundry" defaultBudget={10} />);
    const textarea = screen.getByLabelText("prompt");
    fireEvent.change(textarea, { target: { value: "add health check" } });
    fireEvent.click(screen.getByText("send"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const payload = onSubmit.mock.calls[0][0] as ComposerPayload;
    expect(payload.prompt).toBe("add health check");
    expect(payload.mode).toBe("goal");
    expect(payload.model).toBe("qwen-foundry");
    expect(payload.budgetUsd).toBe(10);
    expect(payload.chips).toEqual([]);
  });

  it("clears the prompt after submit", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const textarea = screen.getByLabelText("prompt") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "do the thing" } });
    fireEvent.click(screen.getByText("send"));
    expect(textarea.value).toBe("");
  });

  it("Enter submits, Shift+Enter inserts newline", () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("prompt");
    fireEvent.change(textarea, { target: { value: "task" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("Shift+Enter does not submit", () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("prompt");
    fireEvent.change(textarea, { target: { value: "task" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("mode selector offers ask/plan/dev/goal", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const modeSelect = screen.getByLabelText("mode") as HTMLSelectElement;
    const options = Array.from(modeSelect.options).map((o) => o.value);
    expect(options).toEqual(["ask", "plan", "development", "goal"]);
  });

  it("budget selector offers 5/10/20/40", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const budgetSelect = screen.getByLabelText("budget") as HTMLSelectElement;
    const values = Array.from(budgetSelect.options).map((o) => Number(o.value));
    expect(values).toEqual([5, 10, 20, 40]);
  });
});
