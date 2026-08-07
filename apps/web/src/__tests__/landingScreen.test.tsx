import { act, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { renderScreen } from "./render";
import { LandingScreen } from "../features/landing/LandingScreen";
import { useSession } from "../stores/session";

function renderLanding() {
  return renderScreen(
    <MemoryRouter>
      <LandingScreen />
    </MemoryRouter>,
  );
}

describe("LandingScreen", () => {
  beforeEach(() => {
    useSession.setState({ me: null, booted: true });
  });

  it("renders the hero headline, aura section, and the demo console", () => {
    renderLanding();
    expect(screen.getByTestId("landing-hero")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Collegium — a legion of swarm agents, moving as one.",
    );
    expect(screen.getByTestId("landing-demo")).toBeInTheDocument();
  });

  it("lists all six blueprints as jack-strip modules", () => {
    renderLanding();
    const strip = screen.getByTestId("landing-blueprints");
    for (const name of ["ask", "plan", "debug", "development", "swarm", "goal"]) {
      expect(within(strip).getByText(name)).toBeInTheDocument();
    }
  });

  it("points the primary CTA at /login when signed out", () => {
    renderLanding();
    expect(screen.getByTestId("landing-primary-cta")).toHaveAttribute("href", "/login");
  });

  it("flips the primary CTA to the console once signed in", () => {
    renderLanding();
    act(() =>
      useSession.setState({
        me: { id: 1, username: "sahil", display_name: "sahil", role: "admin", must_change_pin: false },
      }),
    );
    expect(screen.getByTestId("landing-primary-cta")).toHaveAttribute("href", "/app");
  });
});
