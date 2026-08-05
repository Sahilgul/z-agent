import { beforeEach, describe, expect, it, vi } from "vitest";

// M-90: the approvalQueue "enable push" test mocked subscribeToPush entirely,
// so it could only assert the mock was called — it never verified that the
// REAL subscribeToPush POSTs the subscription to the server. This test
// exercises the real implementation against mocked browser push primitives
// + a mocked api and asserts the /push/subscriptions POST happens with the
// subscription payload.

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

// Browser push primitives — jsdom doesn't ship these, so install them on
// navigator/window before importing the module under test.
function installPushPrimitives() {
  const unsubscribe = vi.fn(async () => true);
  const subscribe = vi.fn(async () => ({
    endpoint: "https://push.example/sub/123",
    toJSON: () => ({
      endpoint: "https://push.example/sub/123",
      keys: { p256dh: "p256dh-key", auth: "auth-key" },
    }),
    unsubscribe,
  }));
  const registration = { pushManager: { subscribe } };
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { getRegistration: async () => registration },
  });
  Object.defineProperty(window, "PushManager", {
    configurable: true,
    value: {},
  });
  return { subscribe, unsubscribe };
}

describe("subscribeToPush", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installPushPrimitives();
    get.mockResolvedValue({ public_key: "BPk_test_key", enabled: true });
    post.mockResolvedValue({});
  });

  it("POSTs the push subscription to /push/subscriptions", async () => {
    const { subscribeToPush } = await import("../lib/push");
    const ok = await subscribeToPush();
    expect(ok).toBe(true);
    // fetched the VAPID public key first
    expect(get).toHaveBeenCalledWith("/push/vapid-public-key");
    // M-90: the server POST that was never asserted before.
    expect(post).toHaveBeenCalledWith("/push/subscriptions", {
      endpoint: "https://push.example/sub/123",
      keys: { p256dh: "p256dh-key", auth: "auth-key" },
    });
  });

  it("returns false (and does not POST) when push is disabled server-side", async () => {
    get.mockResolvedValue({ public_key: "", enabled: false });
    const { subscribeToPush } = await import("../lib/push");
    const ok = await subscribeToPush();
    expect(ok).toBe(false);
    expect(post).not.toHaveBeenCalled();
  });

  it("unsubscribes the orphaned browser subscription when the POST fails (G-27)", async () => {
    // G-27: if the server POST fails, the browser is left subscribed to push
    // while the server has no record — an orphan. The user believes they
    // opted in (toggle says so) but no notifications arrive, and a retry
    // finds the existing subscription and skips re-subscribing — stuck.
    // subscribeToPush must unsubscribe the browser side AND return false so
    // the UI can surface the failure.
    const { unsubscribe } = installPushPrimitives();
    get.mockResolvedValue({ public_key: "BPk_test_key", enabled: true });
    post.mockRejectedValue(new Error("server down"));
    const { subscribeToPush } = await import("../lib/push");
    const ok = await subscribeToPush();
    expect(ok).toBe(false);
    expect(post).toHaveBeenCalledWith("/push/subscriptions", expect.anything());
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });
});
