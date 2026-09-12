import { useCallback, useEffect, useState } from "react";

/**
 * Light / dark, and the choice not to choose.
 *
 * Three states rather than two, because `tokens.css` was already written for three: light
 * lives on bare `:root`, dark is redefined under `prefers-color-scheme` *and* under an
 * explicit `[data-theme="dark"]` stamp, and the dark media block is guarded with
 * `:root:not([data-theme="light"])`. That guard only earns its keep if "no stamp at all" is
 * a reachable state — a two-way toggle that always stamps would make the OS media query
 * dead code and take the follow-my-system behaviour away from people who want it.
 *
 * So: `system` removes the attribute and lets the media query decide; `light` and `dark`
 * stamp it and win in either direction.
 */

export type Theme = "system" | "light" | "dark";

/** Also read by the inline script in index.html. Changing it here means changing it there. */
export const THEME_STORAGE_KEY = "lm.theme";

export const THEMES: { value: Theme; label: string; icon: string }[] = [
  { value: "system", label: "System", icon: "◐" },
  { value: "light", label: "Light", icon: "☀" },
  { value: "dark", label: "Dark", icon: "☾" },
];

function isTheme(value: unknown): value is Theme {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * Storage access is wrapped because it throws, not merely returns null, when site data is
 * blocked — Safari's private mode and locked-down enterprise profiles both do this, and an
 * uncaught throw here would take down the whole shell on mount.
 */
export function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

function store(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem(THEME_STORAGE_KEY);
    else localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // A preference that cannot be persisted still applies for this session.
  }
}

/** The single place the attribute is written, so the DOM cannot disagree with the state. */
export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);

  // The inline script in index.html has already stamped the attribute before first paint;
  // this only re-asserts it, which matters when React re-mounts in development.
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  /**
   * Another tab changed the preference.
   *
   * `storage` fires only in the *other* tabs, which is exactly what is wanted: a console
   * left open on a second monitor should not stay dark after the first tab was set to light.
   */
  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key === THEME_STORAGE_KEY) setThemeState(readStoredTheme());
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setTheme = useCallback((next: Theme) => {
    store(next);
    applyTheme(next);
    setThemeState(next);
  }, []);

  return { theme, setTheme };
}
