import { createAuthClient } from "better-auth/react";

/**
 * Same origin story as `api.ts`: empty base in dev so Vite proxies /api/auth to the backend
 * and the session cookie is first-party.
 */
export const authClient = createAuthClient({
  // `||`, not `??`: an unset var in .env arrives as "" rather than undefined, and `??`
  // would happily pass that empty string through as the base URL.
  baseURL: import.meta.env.VITE_API_URL || window.location.origin,
  basePath: "/api/auth",
});

export const { signIn, signUp, signOut, useSession } = authClient;
