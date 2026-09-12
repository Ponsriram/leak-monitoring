import { useEffect, useRef, useState } from "react";

/**
 * Copy one value to the clipboard.
 *
 * Exists because the values worth copying out of this console — a victim domain, a WHOIS
 * address, an onion URL — are the ones an analyst pastes into a ticket, and selecting text
 * inside a table cell that is also a link is fiddly enough that people retype them instead.
 *
 * The confirmation is a state change on the button itself rather than a toast: a toast for
 * "copied" is noise in a table where the action is repeated twenty times.
 */
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);

  // Clears on unmount, so a row copied and then scrolled out of a re-rendered page does not
  // set state on a component that is gone.
  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function copy(event: React.MouseEvent) {
    // Cells live inside expandable rows; copying must not also toggle the row.
    event.stopPropagation();
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      // The clipboard is permission-gated and absent over plain http on some browsers.
      // Saying so beats a button that silently does nothing and reads as broken.
      setState("failed");
    }
    timer.current = window.setTimeout(() => setState("idle"), 1400);
  }

  const message =
    state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : `${label}: ${value}`;

  return (
    <button
      type="button"
      className={`copy-btn${state === "failed" ? " copy-btn-failed" : ""}`}
      onClick={copy}
      title={message}
      aria-label={state === "idle" ? `${label} ${value}` : message}
    >
      <span aria-hidden="true">
        {state === "copied" ? "✓" : state === "failed" ? "✕" : "⧉"}
      </span>
      {/* Announced on change; the glyph swap alone reaches nobody using a screen reader. */}
      <span className="sr-only" role="status" aria-live="polite">
        {state === "idle" ? "" : message}
      </span>
    </button>
  );
}
