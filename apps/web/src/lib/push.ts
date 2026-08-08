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
  // W10-#5: the WHOLE body is guarded — the VAPID GET and the browser's
  // subscribe() can both throw (offline, denied permission), and an
  // uncaught rejection left the caller's promise hanging with the button
  // stuck. Any failure reads as "not subscribed".
  try {
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
  } catch {
    return false;
  }
}

/** W10-#8: the off switch. Removes the server record AND the browser
 *  subscription — either half alone leaks state (server keeps pushing to a
 *  dead endpoint, or the browser stays subscribed with no server record). */
export async function unsubscribeFromPush(): Promise<boolean> {
  try {
    if (!pushSupported()) return false;
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (!sub) return true; // nothing to undo
    const endpoint = sub.endpoint;
    try {
      await api.delete("/push/subscriptions", { endpoint, keys: {} });
    } catch {
      return false; // keep the browser subscription so state stays consistent
    }
    await sub.unsubscribe();
    return true;
  } catch {
    return false;
  }
}
