/** fetch wrapper — cookie JWT (httponly collegium_token set at login), JSON in/out.
 *  A 401 anywhere means the session died: notify via the session store event. */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;
export function setUnauthorizedHandler(fn: UnauthorizedHandler) {
  onUnauthorized = fn;
}

/** W1-M2: FastAPI 422s carry detail as an ARRAY of {loc, msg} — String() on
 *  it rendered "[object Object]" in every error toast. Join the messages. */
function detailText(detail: unknown): string | null {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) =>
      d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d),
    );
    return msgs.join("; ") || null;
  }
  return null;
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = await res.json();
    return detailText(body?.detail);
  } catch {
    return null; // non-JSON error body — caller falls back to statusText
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    // M-87: spread init BEFORE headers so a caller's init.headers doesn't
    // clobber the merged Content-Type. The old order (...init last) let
    // init.headers overwrite the whole headers object, dropping the JSON
    // default. Now headers is set last and merges the caller's headers on
    // top of the Content-Type default (caller can still override CT).
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (res.status === 401) {
    // W1-M1: a 401 from the LOGIN call means "invalid credentials", not
    // "session expired" — read the server's detail before defaulting. The
    // handler still fires (it only nulls `me`, which is already null on the
    // login screen).
    const detail = await readDetail(res);
    onUnauthorized?.();
    throw new ApiError(401, detail ?? "session expired");
  }
  if (!res.ok) {
    const detail = await readDetail(res);
    throw new ApiError(res.status, detail ?? res.statusText);
  }
  // W10-#8: 204 (the DELETE endpoints) has NO body — res.json() would throw
  // and turn a successful unsubscribe into a fake failure.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "DELETE", body: body === undefined ? undefined : JSON.stringify(body) }),
};
