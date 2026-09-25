import { THEMES, useTheme } from "../lib/theme";

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
            aria-pressed={active}
            title={`Always use the ${option.label.toLowerCase()} theme`}
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