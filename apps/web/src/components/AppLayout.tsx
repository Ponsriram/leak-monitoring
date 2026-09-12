import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { signOut, useSession } from "../lib/auth-client";
import { useLiveUpdates } from "../lib/live";
import { GlobalSearch } from "./GlobalSearch";
import { ThemeToggle } from "./ThemeToggle";

/**
 * The app shell.
 *
 * A layout route: the sidebar lives here and the pages render into <Outlet/>, so it mounts
 * once. The old app imported <Sidebar/> into every page component, which remounted it on
 * every navigation.
 *
 * The nav is grouped rather than flat because the sections answer two different questions —
 * "what happened in the world" and "what indicators do we hold" — and a flat list of eight
 * entries gives no hint which is which.
 */

type NavItem = { to: string; label: string; icon: string; end?: boolean };
type NavGroup = { label: string; items: NavItem[] };

const NAV: NavGroup[] = [
  {
    label: "World Incidents",
    items: [
      { to: "/dashboard", label: "Overview", icon: "▤", end: true },
      { to: "/dashboard/general", label: "General", icon: "☰" },
      { to: "/dashboard/ransomware", label: "Ransomware", icon: "☣" },
      { to: "/dashboard/darkweb", label: "Dark Web", icon: "◍" },
    ],
  },
  {
    label: "Bulk Intelligence",
    items: [
      { to: "/dashboard/search", label: "Search", icon: "⌕" },
      { to: "/dashboard/iocs", label: "IOC", icon: "◎" },
      { to: "/dashboard/leaks", label: "Leaks", icon: "▦" },
    ],
  },
  {
    label: "Operations",
    items: [
      { to: "/dashboard/sources", label: "Sources", icon: "◈" },
      { to: "/dashboard/alerts", label: "Alerts", icon: "◔" },
    ],
  },
];

export function AppLayout() {
  const { data: session } = useSession();
  const navigate = useNavigate();
  // One stream for the whole app, opened here because the shell mounts once. Opening it per
  // page would tear down and re-establish the connection on every navigation.
  const live = useLiveUpdates();

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
          {NAV.map((group) => (
            <div key={group.label} className="nav-group">
              <div className="nav-label">{group.label}</div>
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end}>
                  <span className="nav-icon" aria-hidden="true">
                    {item.icon}
                  </span>
                  {item.label}
                </NavLink>
              ))}
            </div>
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
        <div className="topbar">
          <GlobalSearch />

          {/*
            The live indicator is also the way into the map — it is the one control that is
            about "what is happening right now", and the map is the view of that. Keeping it
            as a sidebar entry made it a peer of the tables, which it is not: it is a lens
            over the same data rather than another section of it.

            It stays a button with a label rather than becoming an icon: colour alone carries
            the connection state for a sighted user and nothing for anyone else.
          */}
          <button
            type="button"
            className={`live-badge${live.connected ? " live-on" : " live-off"}`}
            onClick={() => navigate("/dashboard/map")}
            title={
              live.connected
                ? "Connected — new leaks appear as they are collected. Opens the live threat map."
                : "Reconnecting. Opens the live threat map."
            }
          >
            <span className="live-dot" aria-hidden="true" />
            {live.connected ? "Live" : "Reconnecting"}
            {live.newLeaks > 0 && <span className="live-count">+{live.newLeaks}</span>}
            <span className="live-open" aria-hidden="true">
              ◉
            </span>
          </button>

          <ThemeToggle />
        </div>
        <Outlet />
      </main>
    </div>
  );
}
