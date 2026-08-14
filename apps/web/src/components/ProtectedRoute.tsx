import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useSession } from "../lib/auth-client";
import { Loading } from "./states";

/**
 * Gate for everything behind sign-in.
 *
 * The old app's "login" was a call to `navigateTo('/dashboard')` — no token, nothing stored,
 * no route guard — so typing /dashboard in the address bar skipped the login screen entirely.
 * The API enforces this independently; this is the UX half.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { data: session, isPending } = useSession();
  const location = useLocation();

  // Don't redirect while the session is still resolving, or a refresh on a deep link
  // would bounce the user to /login before the cookie is read.
  if (isPending) return <Loading label="Checking your session…" />;

  if (!session?.user) {
    // Remember where they were headed so sign-in can return them there.
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}
