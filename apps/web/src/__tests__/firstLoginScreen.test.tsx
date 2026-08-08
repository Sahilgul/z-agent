import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FirstLoginScreen } from "../features/login/FirstLoginScreen";
import { renderScreen } from "./render";
import { useSession } from "../stores/session";

// W-H15: the setup-code redemption screen — the only in-app path for the
// codes TeamScreen mints. Pins: posts to /auth/first-login, hydrates the
// session from /auth/me afterwards, surfaces server errors (bad code, weak
// pin) instead of dying silently.

const apiMock = {
  get: vi.fn<(path: string) => Promise<unknown>>(),
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
};

vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return {
    ...orig,
    api: {
      get: (path: string) => apiMock.get(path),
      post: (path: string, body?: unknown) => apiMock.post(path, body),
    },
  };
});

const me = { id: 7, username: "newbie", display_name: "newbie", role: "member" };

describe("FirstLoginScreen", () => {
  beforeEach(() => {
    apiMock.get.mockReset();
    apiMock.post.mockReset();
    useSession.setState({ me: null, booted: true });
    renderScreen(
      <MemoryRouter>
        <FirstLoginScreen />
      </MemoryRouter>,
    );
  });

  function fill(username: string, code: string, pin: string, confirm: string) {
    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: username } });
    fireEvent.change(screen.getByLabelText(/setup code/i), { target: { value: code } });
    fireEvent.change(screen.getByLabelText(/choose a pin/i), { target: { value: pin } });
    fireEvent.change(screen.getByLabelText(/repeat the pin/i), { target: { value: confirm } });
    fireEvent.click(screen.getByRole("button", { name: /set up/i }));
  }

  it("posts the code + chosen pin and hydrates the session", async () => {
    apiMock.post.mockResolvedValueOnce({ username: "newbie", first_run: true });
    apiMock.get.mockResolvedValueOnce(me);

    fill("newbie", "12345678", "4242", "4242");

    await waitFor(() => expect(useSession.getState().me).toEqual(me));
    expect(apiMock.post).toHaveBeenCalledWith("/auth/first-login", {
      username: "newbie",
      code: "12345678",
      pin: "4242",
      display_name: undefined,
    });
    expect(apiMock.get).toHaveBeenCalledWith("/auth/me");
  });

  it("surfaces the server error for a bad/expired code", async () => {
    apiMock.post.mockRejectedValueOnce(new Error("code expired or already used"));

    fill("newbie", "12345678", "4242", "4242");

    expect(await screen.findByRole("alert")).toHaveTextContent("code expired or already used");
    expect(useSession.getState().me).toBeNull();
  });

  it("blocks submit when the pins don't match", async () => {
    fill("newbie", "12345678", "4242", "9999");

    expect(await screen.findByText(/pins don't match/i)).toBeInTheDocument();
    expect(apiMock.post).not.toHaveBeenCalled();
  });
});
