import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventStream } from "../components/EventStream";
import type { StepEvent } from "../types";

const ev = (seq: number, kind: StepEvent["kind"], title: string, detail: Record<string, unknown> = {}, ts = "2026-08-01T00:00:00Z"): StepEvent => ({
  schema_version: 1,
  run_id: "r1",
  thread_id: "l1",
  seq,
  ts,
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

  it("styles the user message as a right-aligned card, the agent reply as plain prose", () => {
    const { container } = render(
      <EventStream
        events={[
          ev(0, "message", "q", { text: "my question", role: "user" }),
          ev(1, "message", "a", { text: "my answer", role: "agent" }),
        ]}
        deltas={[]}
      />,
    );
    const user = container.querySelector("[data-role='user']");
    const agent = container.querySelector("[data-role='agent']");
    // User: compact smart card docked right.
    expect(user?.className).toMatch(/justify-end/);
    expect(user?.querySelector(".rounded-2xl")).not.toBeNull();
    // Agent: plain prose — no card frame, no visible speaker tag.
    expect(agent?.className).not.toMatch(/justify-end/);
    expect(agent?.querySelector(".rounded-2xl")).toBeNull();
    expect(agent?.querySelector(".text-micro")).toBeNull();
  });

  it("renders a failed marker on bad steps", () => {
    render(<EventStream events={[ev(0, "command", "pytest", { ok: false })]} deltas={[]} />);
    expect(screen.getByText(/failed/)).toBeInTheDocument();
  });

  it("stamps every message and shows took-Ns under the agent reply", () => {
    const { container } = render(
      <EventStream
        events={[
          ev(0, "message", "q", { text: "my question", role: "user" }, "2026-08-01T00:00:00Z"),
          ev(1, "message", "a", { text: "my answer", role: "agent" }, "2026-08-01T00:01:10Z"),
        ]}
        deltas={[]}
      />,
    );
    // Both bubbles carry a clock stamp…
    const metas = container.querySelectorAll("[data-testid='msg-meta']");
    expect(metas).toHaveLength(2);
    expect(metas[0].textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(metas[0].textContent).not.toMatch(/took/); // the user asks; the agent takes time
    // …and the agent's reply shows the raw-seconds duration at the bottom.
    expect(metas[1].textContent).toMatch(/took 70s/);
    expect(container.querySelector("[data-role='agent']")?.textContent).toMatch(/took 70s/);
  });

  it("renders the turn-metrics footer under agent messages", () => {
    const { container } = render(
      <EventStream
        events={[
          ev(0, "message", "a", {
            text: "the answer",
            role: "agent",
            metrics: {
              ttft_s: 7.72, latency_s: 9.18, input_tokens: 89,
              output_tokens: 94, reasoning_tokens: 72, cached_tokens: 37,
            },
          }),
        ]}
        deltas={[]}
      />,
    );
    const footer = container.querySelector("[data-testid='msg-metrics']");
    expect(footer?.textContent).toBe(
      "ttft 7.72s · 9.18s · in 89 · out 94 (72 reasoning) · cached 37",
    );
  });

  it("omits the metrics footer when the turn reported none", () => {
    const { container } = render(
      <EventStream
        events={[ev(0, "message", "a", { text: "plain", role: "agent" })]}
        deltas={[]}
      />,
    );
    expect(container.querySelector("[data-testid='msg-metrics']")).toBeNull();
  });

  it("shows no took-Ns on replies with no preceding user message", () => {
    render(
      <EventStream events={[ev(0, "message", "a", { text: "lone reply", role: "agent" })]} deltas={[]} />,
    );
    expect(screen.getByText("lone reply")).toBeInTheDocument();
    expect(screen.queryByText(/took/)).not.toBeInTheDocument();
  });

  it("stamps the prompt header bubble with the run's creation time", () => {
    const { container } = render(
      <EventStream events={[]} deltas={[]} prompt="fix the flaky login" promptTs="2026-08-01T00:00:00Z" />,
    );
    expect(container.querySelector("[data-testid='msg-meta']")?.textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
  });

  it("renders file_read payloads as VS Code-themed code, not raw text", async () => {
    const { container } = render(
      <EventStream
        events={[ev(0, "file_read", "read src/app.py", { text: "def f():\n    return 1" })]}
        deltas={[]}
      />,
    );
    // PrismAsyncLight loads the grammar async — wait for tokenized spans.
    await waitFor(() => expect(container.querySelector("code .token")).not.toBeNull());
    expect(container.textContent).toContain("def f():");
  });

  it("W7-L2: file_edit highlights by file path — the diff-grammar branch is gone", async () => {
    // file_edit emits SUMMARIES by design; a diff-SHAPED body must not flip
    // the grammar (that branch pinned a fixture the worker never produces).
    const diff = ["--- a/x.ts", "+++ b/x.ts", "@@ -1 +1 @@", "-old", "+new"].join("\n");
    const { container } = render(
      <EventStream events={[ev(0, "file_edit", "edit x.ts", { text: diff, path: "x.ts" })]} deltas={[]} />,
    );
    await waitFor(() => expect(container.querySelector("code .token")).not.toBeNull());
    expect(container.textContent).toContain("+new");
  });

  it("W7-M3: windows the rendered tail and offers the full history on tap", () => {
    const many = Array.from({ length: 350 }, (_, i) => ev(i, "message", `step ${i}`, { text: `body ${i}` }));
    render(<EventStream events={many} deltas={[]} />);
    expect(screen.queryByText("body 0")).not.toBeInTheDocument();
    expect(screen.getByText("body 349")).toBeInTheDocument();
    const expander = screen.getByRole("button", { name: /show 50 earlier steps/ });
    fireEvent.click(expander);
    expect(screen.getByText("body 0")).toBeInTheDocument();
  });

  it("renders markdown code fences with syntax highlighting", async () => {
    const { container } = render(
      <EventStream
        events={[ev(0, "message", "a", { text: "try this:\n\n```ts\nconst x: number = 1;\n```" })]}
        deltas={[]}
      />,
    );
    await waitFor(() => expect(container.querySelector("pre code .token")).not.toBeNull());
    expect(container.textContent).toContain("const");
  });

  it("never renders a --- inside a message as a visible rule", () => {
    const { container } = render(
      <EventStream
        events={[ev(0, "message", "parts", { text: "part one\n\n---\n\npart two", role: "agent" })]}
        deltas={[]}
      />,
    );
    expect(container.querySelector("hr")).not.toBeInTheDocument();
    expect(screen.getByText(/part one/)).toBeInTheDocument();
    expect(screen.getByText(/part two/)).toBeInTheDocument();
  });

  it("marks the seam between turns, never the opening turn", () => {
    render(
      <EventStream
        events={[
          ev(0, "message", "first ask", { text: "one", role: "user" }),
          ev(1, "message", "reply", { text: "two", role: "agent" }),
          ev(2, "message", "second ask", { text: "three", role: "user" }),
        ]}
        deltas={[]}
      />,
    );
    expect(screen.getAllByTestId("turn-divider")).toHaveLength(1);
  });
});
