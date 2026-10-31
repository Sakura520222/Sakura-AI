let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(`Request failed (${status})`);
    this.name = "ApiError";
  }
}

/** One same-origin boundary for cookie auth, CSRF, errors and session expiry. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (
    !path.startsWith("/") ||
    path.startsWith("//") ||
    path.includes("\\") ||
    new URL(path, window.location.origin).origin !== window.location.origin
  )
    throw new Error("A same-origin API path is required");
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const method = (init.method ?? "GET").toUpperCase();
  if (init.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken)
    headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const body: unknown =
    response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("sakura:session-expired"));
    if (
      response.status === 428 &&
      body &&
      typeof body === "object" &&
      "detail" in body &&
      body.detail === "MFA enrollment required"
    )
      window.dispatchEvent(new Event("sakura:enrollment-required"));
    throw new ApiError(response.status, body);
  }
  return body as T;
}
