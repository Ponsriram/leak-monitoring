import { useCallback, useEffect, useState } from "react";

/**
 * Supports light, dark, and system themes.
 *
 * The system theme is handled internally and is not displayed
 * as an option in the theme toggle UI.
 *
 * When no theme preference is saved, the app follows the
 * operating system's light or dark preference.
 */

export type Theme = "system" | "light" | "dark";

/** Also read by the inline script in index.html. */
export const THEME_STORAGE_KEY = "lm.theme";

/**
 * Only light and dark are displayed in the theme toggle.
 * The system theme remains available internally.
 */
export const THEMES: {
  value: Theme;
  label: string;
  icon: string;
}[] = [
  { value: "light", label: "Light", icon: "☀" },
  { value: "dark", label: "Dark", icon: "☾" },
];

function isTheme(value: unknown): value is Theme {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * Safely read the saved theme preference.
 * Defaults to system theme when no preference is saved.
 */
export function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

/**
 * Save the theme preference.
 * System theme removes the stored preference.
 */
function store(theme: Theme) {
  try {
    if (theme === "system") {
      localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    }
  } catch {
    // Preference still applies for this session.
  }
}

/**
 * Apply the selected theme to the document.
 *
 * System theme removes the data-theme attribute,
 * allowing CSS prefers-color-scheme to determine the theme.
 */
export function applyTheme(theme: Theme) {
  const root = document.documentElement;

  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", theme);
  }
}

/**
 * Theme hook.
 *
 * Supports:
 * - System theme detection
 * - Manual light/dark selection
 * - Local storage persistence
 * - Cross-tab synchronization
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);

  // Apply the theme whenever it changes.
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  /**
   * Synchronize theme changes made in another browser tab.
   */
  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key === THEME_STORAGE_KEY) {
        setThemeState(readStoredTheme());
      }
    }

    window.addEventListener("storage", onStorage);

    return () => {
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  /**
   * Change the theme.
   */
  const setTheme = useCallback((next: Theme) => {
    store(next);
    applyTheme(next);
    setThemeState(next);
  }, []);

  return {
    theme,
    setTheme,
  };
}