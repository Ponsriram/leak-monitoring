import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { signOut, useSession } from "../lib/auth-client";
import { useLiveUpdates } from "../lib/live";
import { GlobalSearch } from "./GlobalSearch";
import { ThemeToggle } from "./ThemeToggle";
import {
  ChevronDown,
  Crosshair,
  Database,
  Globe,
  LayoutGrid,
  ListIcon,
  Lock,
  LogOut,
  MapIcon,
  Menu,
  Radio,
  Search,
  Shield,
  Smartphone,
} from "./icons";

/**
 * The app shell.
 *
 * A layout route: the sidebar lives here and the pages render into <Outlet/>, so it mounts
 * once instead of remounting on every navigation.
 *
 * The nav is grouped rather than flat because the sections answer two different questions —
 * "what happened in the world" and "what indicators do we hold" — and a flat list of eight
 * entries gives no hint which is which. Groups collapse, as in the reference console, so an
 * analyst who lives in one section can fold the others away.
 */

type NavItem = { to: string; label: string; icon: React.ReactNode; end?: boolean };
type NavGroup = { label: string; items: NavItem[] };

const NAV: NavGroup[] = [
  {
    label: "World Incidents",
    items: [
      { to: "/dashboard", label: "Dashboard", icon: <LayoutGrid />, end: true },
      { to: "/dashboard/general", label: "General", icon: <ListIcon /> },
      { to: "/dashboard/ransomware", label: "Ransomware", icon: <Lock /> },
      { to: "/dashboard/darkweb", label: "Dark Web", icon: <Globe /> },
    ],
  },
  {
    label: "Bulk Intelligence",
    items: [
      { to: "/dashboard/search", label: "Search", icon: <Search /> },
      { to: "/dashboard/iocs", label: "IOC", icon: <Crosshair /> },
      { to: "/dashboard/mobile", label: "Mobile Number", icon: <Smartphone /> },
      { to: "/dashboard/leaks", label: "Leaks", icon: <Database /> },
    ],
  },
  {
    label: "Operations",
    items: [{ to: "/dashboard/sources", label: "Sources", icon: <Radio /> }],
  },
];

const RAIL_KEY = "lm.sidebar.rail";

function readRail(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === "1";
  } catch {
    // Site data blocked — the sidebar simply starts expanded.
    return false;
  }
}

export function AppLayout() {
  const { data: session } = useSession();
  const navigate = useNavigate();
  // One stream for the whole app, opened here because the shell mounts once. Opening it per
  // page would tear down and re-establish the connection on every navigation.
  const live = useLiveUpdates();
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // Icon-only rail, remembered per browser so the choice survives a reload.
  const [rail, setRail] = useState(readRail);

  function toggleRail() {
    setRail((current) => {
      const next = !current;
      try {
        localStorage.setItem(RAIL_KEY, next ? "1" : "0");
      } catch {
        // A preference that cannot be persisted still applies for this visit.
      }
      return next;
    });
  }

  function toggleGroup(label: string) {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  async function handleSignOut() {
    await signOut();
    navigate("/login", { replace: true });
  }

  const initials = (session?.user?.name ?? session?.user?.email ?? "?")
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("");

  return (
    <div className={rail ? "shell shell-rail" : "shell"}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <Shield size={16} />
          </div>
          <div className="brand-name">Leak Monitoring</div>
          <button
            type="button"
            className="rail-toggle"
            onClick={toggleRail}
            aria-expanded={!rail}
            aria-label={rail ? "Expand sidebar" : "Collapse sidebar"}
            title={rail ? "Expand sidebar" : "Collapse sidebar"}
          >
            <Menu size={18} />
          </button>
        </div>

        <nav className="nav" aria-label="Main">
          {NAV.map((group) => {
            // With the labels hidden in rail mode there is no way to reopen a folded group,
            // so the rail always shows every item.
            const open = rail || !collapsed.has(group.label);
            const id = `nav-${group.label.toLowerCase().replace(/\s+/g, "-")}`;
            return (
              <div key={group.label} className="nav-group">
                <button
                  type="button"
                  className="nav-label"
                  aria-expanded={open}
                  aria-controls={id}
                  onClick={() => toggleGroup(group.label)}
                >
                  {group.label}
                  <ChevronDown size={14} className={open ? "nav-chevron" : "nav-chevron closed"} />
                </button>
                <div id={id} className="nav-items" hidden={!open}>
                  {group.items.map((item) => (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      end={item.end}
                      title={rail ? item.label : undefined}
                      aria-label={rail ? item.label : undefined}
                    >
                      <span className="nav-icon">{item.icon}</span>
                      <span className="nav-text">{item.label}</span>
                    </NavLink>
                  ))}
                </div>
              </div>
            );
          })}
        </nav>

        <div className="sidebar-foot">
          {session?.user && (
            <div className="who" title={rail ? session.user.email : undefined}>
              <div className="avatar" aria-hidden="true">
                {initials}
              </div>
              <div className="who-text">
                <div className="who-name">{session.user.name}</div>
                <div className="who-mail">{session.user.email}</div>
              </div>
            </div>
          )}
          <button
            type="button"
            className="signout"
            onClick={handleSignOut}
            title={rail ? "Sign out" : undefined}
            aria-label={rail ? "Sign out" : undefined}
          >
            <LogOut size={15} />
            <span className="nav-text">Sign out</span>
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
            <MapIcon size={14} className="live-open" />
          </button>

          <ThemeToggle />
        </div>
        <Outlet />
      </main>
    </div>
  );
}
