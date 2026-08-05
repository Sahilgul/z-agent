import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventStream } from "../components/EventStream";
import type { StepEvent } from "../types";

const ev = (seq: number, kind: StepEvent["kind"], title: string, detail: Record<string, unknown> = {}): StepEvent => ({
  schema_version: 1,
  run_id: "r1",
  thread_id: "l1",
  seq,
  ts: "2026-08-01T00:00:00Z",
  kind,
  title,
  detail,
  sdk_message_uuid: null,
});

describe("EventStream", () => {
  it("renders stored events with titles", () => {
    render(
      <EventStream
        events={[ev(0, "command", "grep dedupe", { output: "hit" }), ev(1, "message", "answer")]}
        deltas={[]}
      />,
    );
    expect(screen.getByText("grep dedupe")).toBeInTheDocument();
    expect(screen.getByText("answer")).toBeInTheDocument();
  });

  it("shows the empty state with no trace", () => {
    render(<EventStream events={[]} deltas={[]} />);
    expect(screen.getByText(/no trace yet/)).toBeInTheDocument();
  });

  it("filters to one thread in overlay mode", () => {
    const other = { ...ev(0, "command", "other thread cmd"), thread_id: "l2" };
    render(<EventStream events={[ev(0, "command", "my cmd"), other]} deltas={[]} laneFilter="l1" />);
    expect(screen.getByText("my cmd")).toBeInTheDocument();
    expect(screen.queryByText("other thread cmd")).not.toBeInTheDocument();
  });

  it("renders markdown in messages instead of printing it raw", () => {
    render(
      <EventStream
        events={[ev(0, "message", "greeting", { text: "hello **Sahil** and `code`" })]}
        deltas={[]}
      />,
    );
    expect(screen.getByText("Sahil").tagName).toBe("STRONG");
    expect(screen.getByText("code").tagName).toBe("CODE");
    expect(screen.queryByText(/\*\*Sahil\*\*/)).not.toBeInTheDocument();
  });

  it("renders GFM tables into a scrollable frame", () => {
    const table = ["| thread | cost |", "| --- | --- |", "| researcher | $0.02 |"].join("\n");
    const { container } = render(
      <EventStream events={[ev(0, "message", "costs", { text: table })]} deltas={[]} />,
    );
    expect(container.querySelector("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "thread" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "researcher" })).toBeInTheDocument();
    // Wide tables must scroll in their own frame; the stream pane clips X.
    expect(container.querySelector(".md-scroll > table")).toBeInTheDocument();
  });

  it("keeps both speakers in the left column, tagged and full width", () => {
    const { container } = render(
      <EventStream
        events={[
          ev(0, "message", "q", { text: "my question", role: "user" }),
          ev(1, "message", "a", { text: "my answer", role: "agent" }),
        ]}
        deltas={[]}
      />,
    );
    expect(screen.getByText("you")).toBeInTheDocument();
    expect(screen.getByText("agent")).toBeInTheDocument();
    // Neither bubble may re-introduce right alignment or a width cap.
    for (const el of container.querySelectorAll("[data-kind='message']")) {
      expect(el.className).not.toMatch(/justify-end|max-w-\[/);
    }
  });

  it("renders a failed marker on bad steps", () => {
    render(<EventStream events={[ev(0, "command", "pytest", { ok: false })]} deltas={[]} />);
    expect(screen.getByText(/failed/)).toBeInTheDocument();
  });
});
