import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { signOut, useSession } from "../lib/auth-client";

const NAV = [
  { to: "/dashboard", label: "Overview", end: true },
  { to: "/dashboard/leaks", label: "Leaks" },
  { to: "/dashboard/sources", label: "Sources" },
  { to: "/dashboard/alerts", label: "Alerts" },
];

/**
 * The app shell.
 *
 * This is a layout route: the sidebar lives here and the pages render into <Outlet/>, so it
 * mounts once. The old app imported <Sidebar/> into every single page component, which
 * remounted it on every navigation.
 */
export function AppLayout() {
  const { data: session } = useSession();
  const navigate = useNavigate();

  async function handleSignOut() {
    await signOut();
    navigate("/login", { replace: true });
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            LM
          </div>
          <div className="brand-name">Leak Monitoring</div>
        </div>

        <nav className="nav" aria-label="Main">
          <div className="nav-label">Console</div>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          {session?.user && (
            <div className="who">
              <div className="who-name">{session.user.name}</div>
              <div className="who-mail">{session.user.email}</div>
            </div>
          )}
          <button type="button" className="btn btn-sm" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
