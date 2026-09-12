import { THEMES, useTheme } from "../lib/theme";

/**
 * System / Light / Dark, as a segmented control.
 *
 * Three visible buttons rather than one cycling button: a control that cycles hides what the
 * other states even are, and with three of them you cannot get to the one you want without
 * guessing how many presses it takes.
 *
 * `.segmented` is the app's existing picker — the map's period control uses it — so this
 * introduces no new styling, and it inherits the focus ring and the active treatment.
 *
 * The icons are decorative. The label is always rendered beside them and is what carries the
 * meaning, on the same rule as every chip in this console; at narrow widths the label is
 * clipped visually but stays in the accessible name via `aria-label` on the group.
 */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();

  return (
    <div
      className="segmented theme-toggle"
      role="group"
      aria-label="Colour theme"
    >
      {THEMES.map((option) => {
        const active = theme === option.value;
        return (
          <button
            key={option.value}
            type="button"
            className={active ? "active" : undefined}
            // Communicates the choice to assistive tech; the .active class is only paint.
            aria-pressed={active}
            title={
              option.value === "system"
                ? "Follow the operating system's light or dark setting"
                : `Always use the ${option.label.toLowerCase()} theme`
            }
            onClick={() => setTheme(option.value)}
          >
            <span className="theme-icon" aria-hidden="true">
              {option.icon}
            </span>
            <span className="theme-label">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}
