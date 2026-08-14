import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { signIn, signUp, useSession } from "../../lib/auth-client";

type Mode = "signin" | "signup";

export function LoginPage() {
  const { data: session, isPending } = useSession();
  const navigate = useNavigate();
  const location = useLocation() as { state?: { from?: string } };

  const [mode, setMode] = useState<Mode>("signin");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const destination = location.state?.from ?? "/dashboard";

  if (isPending) return <div className="state">Checking your session…</div>;
  if (session?.user) return <Navigate to={destination} replace />;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);

    try {
      const result =
        mode === "signin"
          ? await signIn.email({ email, password })
          : await signUp.email({ email, password, name });

      if (result.error) {
        // Surface what actually went wrong — the old login showed one generic
        // "Credentials does not exist!" for every failure mode, including network errors.
        setError(result.error.message ?? "Sign in failed.");
        return;
      }
      navigate(destination, { replace: true });
    } catch {
      setError("Could not reach the server. Is the API running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login-card">
        <div className="login-head">
          <div className="brand">
            <div className="brand-mark" aria-hidden="true">
              LM
            </div>
            <div className="brand-name">Leak Monitoring</div>
          </div>
          <h1>{mode === "signin" ? "Sign in" : "Create an account"}</h1>
        </div>

        {error && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          {mode === "signup" && (
            <div className="field">
              <label htmlFor="name">Name</label>
              <input
                id="name"
                type="text"
                autoComplete="name"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
          )}

          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              required
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {mode === "signup" && (
              <span style={{ color: "var(--muted)", fontSize: 12 }}>
                At least 12 characters.
              </span>
            )}
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={busy}
          >
            {busy ? "Working…" : mode === "signin" ? "Sign in" : "Create account"}
          </button>
        </form>

        <div className="form-switch">
          {mode === "signin" ? "No account yet? " : "Already have an account? "}
          <button
            type="button"
            className="link-btn"
            onClick={() => {
              setMode(mode === "signin" ? "signup" : "signin");
              setError(null);
            }}
          >
            {mode === "signin" ? "Create one" : "Sign in"}
          </button>
        </div>
      </div>
    </div>
  );
}
