/**
 * The one place in the app that knows how to reach the API.
 *
 * The old client hardcoded `http://localhost:5000` and `http://localhost:5001` across nine
 * call sites in seven files, mixing fetch and axios, so the app could not be deployed
 * anywhere without an edit pass. Everything now goes through here.
 *
 * In development `VITE_API_URL` is unset, so requests go to the app's own origin and Vite's
 * proxy forwards /api to the backend — no CORS, and the session cookie stays first-party.
 * In production set VITE_API_URL to the API origin.
 */
// `||` rather than `??`: an unset var in .env arrives as an empty string, not undefined.
// Empty is the correct value here (same-origin), but be explicit about why.
const BASE = import.meta.env.VITE_API_URL || "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type ErrorBody = { error?: string; message?: string; requestId?: string };

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    // Sends the session cookie. Without this every authenticated request 401s.
    credentials: "include",
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (response.status === 204) return undefined as T;

  if (!response.ok) {
    let body: ErrorBody = {};
    try {
      body = (await response.json()) as ErrorBody;
    } catch {
      // Non-JSON error (a proxy 502, say). Fall through to the generic message.
    }
    throw new ApiError(
      response.status,
      body.error ?? "request_failed",
      body.message ?? `Request failed with ${response.status}`,
      body.requestId,
    );
  }

  return (await response.json()) as T;
}

/** Build a query string, dropping empty values so the URL stays clean. */
export function qs(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const str = search.toString();
  return str ? `?${str}` : "";
}
