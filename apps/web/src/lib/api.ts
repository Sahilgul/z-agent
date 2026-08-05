/** fetch wrapper — cookie JWT (httponly zagent_token set at login), JSON in/out.
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
    onUnauthorized?.();
    throw new ApiError(401, "session expired");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, String(detail));
  }
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) }),
};
