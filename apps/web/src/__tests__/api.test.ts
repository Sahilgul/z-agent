import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../lib/api";

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(body), {
        status,
        statusText: `HTTP ${status}`,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("api error rendering (W1-M1 / W1-M2)", () => {
  it("a login 401 surfaces the server's detail, not 'session expired'", async () => {
    mockFetch(401, { detail: "invalid credentials" });
    const err = await api.post("/auth/login", { username: "x", pin: "y", remember: false }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).message).toBe("invalid credentials");
  });

  it("a bare 401 still falls back to 'session expired'", async () => {
    mockFetch(401, {});
    const err = await api.get("/runs").catch((e) => e);
    expect((err as ApiError).message).toBe("session expired");
  });

  it("a 422 validation array joins the msg fields instead of '[object Object]'", async () => {
    mockFetch(422, {
      detail: [
        { loc: ["body", "task"], msg: "field required", type: "missing" },
        { loc: ["body", "mode"], msg: "not a valid mode", type: "value_error" },
      ],
    });
    const err = await api.post("/runs", {}).catch((e) => e);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).message).toBe("field required; not a valid mode");
  });

  it("a string detail passes through unchanged", async () => {
    mockFetch(409, { detail: "run is terminal" });
    const err = await api.post("/runs/r1/intent", {}).catch((e) => e);
    expect((err as ApiError).message).toBe("run is terminal");
  });
});
