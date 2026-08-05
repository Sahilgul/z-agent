import { api } from "./api";

/** PWA push opt-in: asked ONLY after the first AwaitingYou
 *  moment — never on landing. Deep links land on the specific action card. */

export function pushSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window
  );
}

function urlB64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export async function hasSubscription(): Promise<boolean> {
  if (!pushSupported()) return false;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return false;
  return (await reg.pushManager.getSubscription()) !== null;
}

export async function subscribeToPush(): Promise<boolean> {
  if (!pushSupported()) return false;
  const { public_key, enabled } = await api.get<{ public_key: string; enabled: boolean }>(
    "/push/vapid-public-key"
  );
  if (!enabled || !public_key) return false;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return false;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlB64ToUint8Array(public_key) as BufferSource,
  });
  const json = sub.toJSON() as { endpoint?: string; keys?: Record<string, string> };
  try {
    await api.post("/push/subscriptions", {
      endpoint: json.endpoint ?? sub.endpoint,
      keys: json.keys ?? {},
    });
  } catch {
    // G-27: the POST failed — the browser is now subscribed to push but the
    // server has no record of it (an orphan). Left in place, the user
    // believes they opted in (the toggle says so) but no notifications
    // arrive, and a later retry would find an existing subscription and
    // skip re-subscribing — stuck. Unsubscribe the browser side so its
    // state matches the server (not opted in) and surface the failure
    // (return false) so the UI can tell the user it didn't take.
    try {
      await sub.unsubscribe();
    } catch {
      /* best-effort cleanup; the surfaced false is the signal */
    }
    return false;
  }
  return true;
}
