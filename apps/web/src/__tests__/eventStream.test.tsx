import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventStream } from "../components/EventStream";
import type { StepEvent } from "../types";

const ev = (seq: number, kind: StepEvent["kind"], title: string, detail: Record<string, unknown> = {}): StepEvent => ({
  schema_version: 1,
  run_id: "r1",
  lane_id: "l1",
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

  it("filters to one lane in overlay mode", () => {
    const other = { ...ev(0, "command", "other lane cmd"), lane_id: "l2" };
    render(<EventStream events={[ev(0, "command", "my cmd"), other]} deltas={[]} laneFilter="l1" />);
    expect(screen.getByText("my cmd")).toBeInTheDocument();
    expect(screen.queryByText("other lane cmd")).not.toBeInTheDocument();
  });

  it("renders a failed marker on bad steps", () => {
    render(<EventStream events={[ev(0, "command", "pytest", { ok: false })]} deltas={[]} />);
    expect(screen.getByText(/failed/)).toBeInTheDocument();
  });
});
