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
  const [showPassword, setShowPassword] = useState(false);
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
            <div className="password-field">
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                required
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              {/*
                A show/hide toggle so a typo in a masked field can be checked before submitting.
                It only flips the input type on the client — the password is never sent anywhere
                to reveal it. Not in the tab order's way, and it announces its state to a reader.
              */}
              <button
                type="button"
                className="password-toggle"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                aria-pressed={showPassword}
                title={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? <EyeOffIcon /> : <EyeIcon />}
              </button>
            </div>
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

/** Inline SVGs so the toggle needs no icon font or external asset. `currentColor` themes them. */
function EyeIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9.9 4.24A9.1 9.1 0 0 1 12 4c6.5 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.47" />
      <path d="M6.6 6.6A13.3 13.3 0 0 0 2 11s3.5 7 10 7a9 9 0 0 0 5.4-1.6" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="m2 2 20 20" />
    </svg>
  );
}
