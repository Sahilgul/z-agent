import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ErrorBoundary } from "../components/ErrorBoundary";

function Boom({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error("screen crashed");
  return <div data-testid="child">child rendered</div>;
}

describe("ErrorBoundary", () => {
  it("shows the fallback when a child throws", () => {
    render(
      <ErrorBoundary resetKey="/a">
        <Boom shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.queryByTestId("child")).not.toBeInTheDocument();
    expect(screen.getByText("screen crashed")).toBeInTheDocument();
  });

  it("C-18: resets on navigation (resetKey change) so a screen error doesn't trap the session", () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Boom shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.queryByTestId("child")).not.toBeInTheDocument();
    // Navigate to a new route -> resetKey changes -> boundary clears and
    // re-renders the (now healthy) children.
    rerender(
      <ErrorBoundary resetKey="/b">
        <Boom shouldThrow={false} />
      </ErrorBoundary>
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
    expect(screen.queryByText("screen crashed")).not.toBeInTheDocument();
  });

  it("stays in the error state when resetKey is unchanged (no false reset)", () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Boom shouldThrow />
      </ErrorBoundary>
    );
    rerender(
      <ErrorBoundary resetKey="/a">
        <Boom shouldThrow={false} />
      </ErrorBoundary>
    );
    // Same resetKey -> the error persists (the boundary does not reset).
    expect(screen.queryByTestId("child")).not.toBeInTheDocument();
  });
});
